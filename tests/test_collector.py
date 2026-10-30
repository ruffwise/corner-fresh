"""Collector tests against a fake HTTP transport (no network).

Each guard has a bad-case test and, where it applies, an absent-data test.
"""
from __future__ import annotations

import email.utils
import gzip
import json
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collector import close, diag, ledger, mapping  # noqa: E402
from collector.af import AF  # noqa: E402
from collector.config import load  # noqa: E402
from collector.http import Fetcher, canonical  # noqa: E402
from collector.op import OP, QuotaRefused  # noqa: E402
from collector.store import Archive, DatabaseMissing, init_db, now_ms, open_db  # noqa: E402

AF_KEY = "AFSECRET-1234567890"
OP_KEY = "OPSECRET-abcdefghij"
H = 3_600_000


class Fake:
    """Routes (path, frozenset(query minus secrets)) -> list of (status, body) served in order."""

    def __init__(self):
        self.routes: dict[tuple[str, frozenset], list] = {}
        self.calls: list[tuple[str, dict, dict]] = []

    def add(self, path, query, *responses):
        self.routes.setdefault((path, frozenset((k, str(v)) for k, v in query.items())), []).extend(responses)

    def __call__(self, url, headers, timeout):
        u = urllib.parse.urlparse(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((u.path, q, headers))
        key = (u.path, frozenset((k, v) for k, v in q.items() if k != "apiKey"))
        queue = self.routes.get(key)
        if not queue:
            return 404, {"date": email.utils.formatdate(usegmt=True)}, b'{"message":"no fake route"}'
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return status, {"date": email.utils.formatdate(usegmt=True), "x-ratelimit-remaining": "100"}, \
            json.dumps(body).encode()

    def count(self, path):
        return sum(1 for p, _, _ in self.calls if p == path)


def af_page(response, current=1, total=1, errors=None):
    return {"get": "x", "parameters": {}, "errors": errors if errors is not None else [], "results": len(response),
            "paging": {"current": current, "total": total}, "response": response}


def af_fixture(fid, ko_ms, status="NS", home=(1, "Home FC"), away=(2, "Away FC"), league=39, season=2026,
               round_="Regular Season - 9", ft=(None, None)):
    return {"fixture": {"id": fid, "timestamp": ko_ms // 1000, "status": {"short": status}},
            "league": {"id": league, "season": season, "round": round_},
            "teams": {"home": {"id": home[0], "name": home[1]}, "away": {"id": away[0], "name": away[1]}},
            "score": {"fulltime": {"home": ft[0], "away": ft[1]}}}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_KEY", AF_KEY)
    monkeypatch.setenv("OP_KEY", OP_KEY)
    (tmp_path / "config.toml").write_text(
        'db_path = "data/c.db"\narchive_dir = "data/archive"\nreview_dir = "data/review"\n'
        'active_jobs = ["af_fixtures", "af_odds", "af_results", "op_account", "op_fixtures", "op_history"]\n'
        '[[leagues]]\nleague_id = 39\nseason = 2026\noddspapi_tournament_id = 17\n'
        '[oddspapi]\nmonthly_billable_budget = 5\nquota_reserve = 50\n')
    cfg = load(tmp_path / "config.toml")
    con = init_db(cfg.db_path)
    fake = Fake()
    fetcher = Fetcher(con, Archive(cfg.archive_dir), transport=fake, sleep=lambda s: None, secrets=(AF_KEY, OP_KEY))
    fake.add("/leagues", {"id": 39, "season": 2026},
             (200, af_page([{"league": {"id": 39, "name": "Premier League", "type": "League"},
                             "country": {"name": "England"}}])))
    return cfg, con, fake, fetcher


def run(con, job, fn):
    with ledger.run_slot(con, job, scheduled_for=now_ms(), unplanned_ok=True) as slot:
        return fn(slot)


# --- raw recording ---------------------------------------------------------------------

def test_secrets_never_stored(env):
    cfg, con, fake, f = env
    fake.add("/status", {}, (200, af_page({"account": "x"})))
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 3}))
    run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    run(con, "op_account", lambda s: OP(cfg, con, f).account(s))
    blob = b"".join(gzip.decompress(p.read_bytes()) for p in cfg.archive_dir.rglob("*.json.gz"))
    blob += "\n".join(map(str, con.execute("SELECT * FROM api_request").fetchall())).encode()
    assert AF_KEY.encode() not in blob and OP_KEY.encode() not in blob
    assert any(h.get("x-apisports-key") == AF_KEY for _, _, h in fake.calls)   # but it was sent
    assert any(q.get("apiKey") == OP_KEY for _, q, _ in fake.calls)


def test_canonical_refuses_secret_param():
    with pytest.raises(ValueError):
        canonical({"apiKey": "x"})


def test_archive_envelope_rebuilds_row(env):
    cfg, con, fake, f = env
    fake.add("/status", {}, (200, af_page({"ok": 1})))
    run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    path, sha, status = con.execute("SELECT archive_path, sha256, http_status FROM raw_response").fetchone()
    env_ = Archive.read(Path(path))
    assert env_["body_sha256"] == sha and env_["http_status"] == status and "requested_at_ms" in env_


def test_network_failure_is_recorded_not_lost(env):
    cfg, con, fake, f = env

    def boom(url, headers, timeout):
        raise ConnectionError("reset")
    f.transport = boom
    with pytest.raises(ledger.SlotIncomplete):
        run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    rows = con.execute("SELECT http_status, usable FROM raw_response").fetchall()
    assert rows == [(0, 0)] * 3      # three attempts, each recorded


# --- api-football pagination and errors ---------------------------------------------------------

def seed_fixture(env, fid=1001, ko=None, **kw):
    cfg, con, fake, f = env
    ko = ko or (now_ms() + 24 * H) // 1000 * 1000   # API timestamps are whole seconds
    fake.add("/fixtures", {"league": 39, "season": 2026}, (200, af_page([af_fixture(fid, ko, **kw)])))
    run(con, "af_fixtures", lambda s: AF(cfg, con, f).fixtures(s))
    return ko


def test_odds_all_pages_fetched(env):
    cfg, con, fake, f = env
    ko = seed_fixture(env)
    day = time.strftime("%Y-%m-%d", time.gmtime(ko / 1000))
    fake.add("/odds", {"league": 39, "season": 2026, "date": day}, (200, af_page([{}], 1, 2)))
    fake.add("/odds", {"league": 39, "season": 2026, "date": day, "page": 2}, (200, af_page([{}], 2, 2)))
    run(con, "af_odds", lambda s: AF(cfg, con, f).odds(s))
    assert con.execute("SELECT status, expected_pages FROM api_request WHERE endpoint='/odds'").fetchone() == ("complete", 2)


def test_odds_missing_page_fails_slot(env):            # ABSENT page 2
    cfg, con, fake, f = env
    ko = seed_fixture(env)
    day = time.strftime("%Y-%m-%d", time.gmtime(ko / 1000))
    fake.add("/odds", {"league": 39, "season": 2026, "date": day}, (200, af_page([{}], 1, 2)))
    fake.add("/odds", {"league": 39, "season": 2026, "date": day, "page": 2}, (500, {"x": 1}))
    with pytest.raises(ledger.SlotIncomplete):
        run(con, "af_odds", lambda s: AF(cfg, con, f).odds(s))
    assert con.execute("SELECT status FROM api_request WHERE endpoint='/odds'").fetchone() == ("failed",)
    assert con.execute("SELECT count(*) FROM raw_response r JOIN api_request a USING (request_id)"
                       " WHERE a.endpoint='/odds'").fetchone()[0] == 4   # page 1 + 3 attempts at page 2
    assert con.execute("SELECT status FROM capture_slot WHERE job='af_odds'").fetchone() == ("failed",)


def test_api_error_dict_is_not_usable(env):
    cfg, con, fake, f = env
    fake.add("/status", {}, (200, af_page([], errors={"token": "invalid"})))
    with pytest.raises(ledger.SlotIncomplete):
        run(con, "af_bootstrap", lambda s: AF(cfg, con, f).call(s, "/status", {}, "status"))
    assert con.execute("SELECT DISTINCT usable FROM raw_response").fetchall() == [(0,)]


def test_odds_with_no_upcoming_fixtures_is_explained(env):   # ABSENT work
    cfg, con, fake, f = env
    run(con, "af_odds", lambda s: AF(cfg, con, f).odds(s))
    assert con.execute("SELECT status, expected_requests, note FROM capture_slot WHERE job='af_odds'").fetchone() \
        == ("complete", 0, "no work in window")


# --- fixtures, reschedules, outcomes -------------------------------------------------------------

def test_state_obs_only_on_change_and_reschedule_recorded(env):
    cfg, con, fake, f = env
    ko = seed_fixture(env)
    fake.routes.clear()
    fake.add("/fixtures", {"league": 39, "season": 2026}, (200, af_page([af_fixture(1001, ko)])))
    run(con, "af_fixtures", lambda s: AF(cfg, con, f).fixtures(s))
    assert con.execute("SELECT count(*) FROM fixture_state_obs").fetchone()[0] == 1
    fake.routes.clear()
    fake.add("/fixtures", {"league": 39, "season": 2026}, (200, af_page([af_fixture(1001, ko + 24 * H)])))
    run(con, "af_fixtures", lambda s: AF(cfg, con, f).fixtures(s))
    assert con.execute("SELECT kickoff_ms FROM v_fixture_latest WHERE fixture_id=1001").fetchone()[0] == ko + 24 * H


def test_et_rule():
    assert AF.et_rule("League", "Regular Season - 3")[0] == 0
    assert AF.et_rule("Cup", "League Stage - 2")[0] == 0
    assert AF.et_rule("Cup", "Round of 16")[0] == 1
    assert AF.et_rule("Cup", "")[0] == 1           # unknown -> conservative


def finished_setup(env, ko, corners_value):
    cfg, con, fake, f = env
    seed_fixture(env, ko=ko)
    fake.add("/fixtures", {"ids": "1001"}, (200, af_page([af_fixture(1001, ko, status="FT", ft=(2, 1))])))
    stats = [] if corners_value is None else [
        {"team": {"id": 1}, "statistics": [{"type": "Corner Kicks", "value": corners_value[0]}]},
        {"team": {"id": 2}, "statistics": [{"type": "Corner Kicks", "value": corners_value[1]}]}]
    fake.add("/fixtures/statistics", {"fixture": 1001}, (200, af_page(stats)))
    return run(con, "af_results", lambda s: AF(cfg, con, f).results(s))


def test_outcome_recorded_with_corners(env):
    r = finished_setup(env, now_ms() - 3 * H, (7, 3))
    assert r["outcomes_recorded"] == 1
    assert env[1].execute("SELECT goals_home_90, goals_away_90, corners_home, corners_away FROM outcome_obs").fetchone() \
        == (2, 1, 7, 3)


def test_outcome_waits_for_missing_statistics(env):     # ABSENT corners: retry, don't record nulls
    r = finished_setup(env, now_ms() - 3 * H, None)
    assert r == {"checked": 1, "outcomes_recorded": 0, "awaiting_statistics": 1}


def test_outcome_after_24h_records_null_corners(env):
    r = finished_setup(env, now_ms() - 30 * H, None)
    assert r["outcomes_recorded"] == 1
    assert env[1].execute("SELECT corners_home FROM outcome_obs").fetchone() == (None,)


# --- OddsPapi quota guard ------------------------------------------------------------------------

def listing(tid=17, items=None):
    return items if items is not None else [
        {"fixtureId": "idA", "participant1Id": 17, "participant2Id": 44, "participant1Name": "Home FC",
         "participant2Name": "Away FC", "startTime": "2026-10-10T16:30:00Z", "statusId": 2, "tournamentId": tid}]


def test_quota_absent_reading_refuses(env):            # ABSENT quota fields
    cfg, con, fake, f = env
    fake.add("/v4/account", {}, (200, {"plan": "free"}))
    with pytest.raises(QuotaRefused, match="lacks"):
        run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s, force=[17]))
    assert fake.count("/v4/fixtures") == 0


def test_quota_account_failure_refuses(env):           # ABSENT quota reading
    cfg, con, fake, f = env
    fake.add("/v4/account", {}, (500, {"err": 1}))
    with pytest.raises(QuotaRefused, match="failed"):
        run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s, force=[17]))
    assert fake.count("/v4/fixtures") == 0


def test_quota_near_limit_refuses(env):
    cfg, con, fake, f = env
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 200}))
    with pytest.raises(QuotaRefused, match="headroom"):
        run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s, force=[17]))
    assert fake.count("/v4/fixtures") == 0


def test_our_monthly_budget_refuses(env):
    cfg, con, fake, f = env
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing()))
    for _ in range(5):
        run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s, force=[17]))
    with pytest.raises(QuotaRefused, match="monthly budget"):
        run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s, force=[17]))
    assert fake.count("/v4/fixtures") == 5


def test_billable_call_not_retried(env):
    cfg, con, fake, f = env
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (500, {"err": 1}))
    with pytest.raises(ledger.SlotIncomplete):
        run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s, force=[17]))
    assert fake.count("/v4/fixtures") == 1


def test_fixture_listing_not_repeated_within_refresh_window(env):
    cfg, con, fake, f = env
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing()))
    r1 = run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s))
    r2 = run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s))
    assert r1["due"] == [17] and r2 == {"due": [], "listed": 0}
    assert fake.count("/v4/fixtures") == 1 and fake.count("/v4/account") == 1


# --- OddsPapi history scheduling ------------------------------------------------------------------

def mapped_candidate(env, tmp_path, fid="idA"):
    """One api-football fixture, its OddsPapi candidate, a reviewed team map, a live mapping."""
    cfg, con, fake, f = env
    seed_fixture(env, ko=close.iso_ms("2026-10-10T16:30:00Z"))
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing(items=[dict(listing()[0], fixtureId=fid)])))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s))
    csv_path = tmp_path / "teams.csv"
    mapping.propose_teams(cfg, con, csv_path)
    mapping.load_teams(con, csv_path, "c" * 40)
    assert mapping.apply(cfg, con) == {"mapped": 1}
    return OP(cfg, con, f)


HIST = ("/v4/historical-odds", {"fixtureId": "idA", "bookmakers": "pinnacle"})


def test_history_due_then_done(env, tmp_path):
    cfg, con, fake, f = env
    op = mapped_candidate(env, tmp_path)
    assert op.history_due(now_ms()) == ["idA"]
    fake.add(*HIST, (200, {"fixtureId": "idA", "bookmakers": {}}))
    assert run(con, "op_history", lambda s: op.history(s))["usable"] == 1
    assert op.history_due(now_ms()) == []


def test_unmapped_candidates_are_never_fetched(env):          # the 27 Oct failure
    cfg, con, fake, f = env
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing()))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s))
    op = OP(cfg, con, f)
    assert op.history_due(now_ms()) == []
    assert run(con, "op_history", lambda s: op.history(s))["requested"] == 0
    assert fake.count("/v4/historical-odds") == 0


def test_history_transport_failure_stays_due(env, tmp_path):   # ABSENT history: must retry
    cfg, con, fake, f = env
    op = mapped_candidate(env, tmp_path)
    fake.add(*HIST, (503, {"x": 1}))
    with pytest.raises(ledger.SlotIncomplete):
        run(con, "op_history", lambda s: op.history(s))
    assert fake.count("/v4/historical-odds") == 3                  # 5xx is retried
    assert op.history_due(now_ms()) == ["idA"]


def test_history_404_is_settled_not_retried_and_slot_completes(env, tmp_path):
    cfg, con, fake, f = env
    op = mapped_candidate(env, tmp_path)
    fake.add(*HIST, (404, {"message": "not found"}))
    r = run(con, "op_history", lambda s: op.history(s))
    assert r["no_history"] == 1 and fake.count("/v4/historical-odds") == 1     # no retry
    assert con.execute("SELECT status FROM capture_slot WHERE job='op_history'").fetchone() == ("complete",)
    assert con.execute("SELECT outcome FROM request_outcome").fetchall() == [("no_history",)]
    t = now_ms()
    assert op.history_due(t) == [] and op.history_due(t + 23 * H) == []        # not re-queued hourly
    assert op.history_due(t + 25 * H) == ["idA"]                              # asked once more after 24 h
    with pytest.MonkeyPatch.context() as mp:
        import collector.op as opmod
        mp.setattr(opmod, "now_ms", lambda: t + 25 * H)
        run(con, "op_history", lambda s: op.history(s))
    assert op.history_due(t + 100 * H) == []                                  # terminal after the 2nd 404


def test_strictly_terminal_404_when_configured(env, tmp_path):
    import dataclasses
    cfg, con, fake, f = env
    op = mapped_candidate(env, tmp_path)
    op.cfg = dataclasses.replace(cfg, op_history_404_max=1)
    fake.add(*HIST, (404, {"message": "not found"}))
    run(con, "op_history", lambda s: op.history(s))
    assert op.history_due(now_ms() + 1000 * H) == []


def test_history_429_backs_off_then_succeeds(env, tmp_path):
    cfg, con, fake, f = env
    slept = []
    f.sleep = slept.append
    op = mapped_candidate(env, tmp_path)
    fake.add(*HIST, (429, {"message": "slow down"}), (200, {"fixtureId": "idA", "bookmakers": {}}))
    assert run(con, "op_history", lambda s: op.history(s))["usable"] == 1
    assert 12.0 in slept                                            # 2 x the 6 s pacing


def test_history_stops_after_two_rate_limited_fixtures(env, tmp_path):
    cfg, con, fake, f = env
    op = mapped_candidate(env, tmp_path)
    fake.add("/v4/historical-odds", {"fixtureId": "x1", "bookmakers": "pinnacle"}, (429, {}))
    fake.add("/v4/historical-odds", {"fixtureId": "x2", "bookmakers": "pinnacle"}, (429, {}))
    with pytest.raises(ledger.SlotIncomplete):
        run(con, "op_history", lambda s: op.history(s, ids=["x1", "x2", "x3"]))
    assert fake.count("/v4/historical-odds") == 6                  # x3 never asked


def test_request_outcome_refuses_anything_but_a_genuine_404(env, tmp_path):
    cfg, con, fake, f = env
    op = mapped_candidate(env, tmp_path)
    fake.add(*HIST, (503, {}))
    with pytest.raises(ledger.SlotIncomplete):
        run(con, "op_history", lambda s: op.history(s))
    req = con.execute("SELECT request_id FROM api_request WHERE endpoint='/v4/historical-odds'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="recorded 404"):
        con.execute("INSERT INTO request_outcome VALUES (?, 'no_history', 404, 1)", (req,))
    other = con.execute("SELECT request_id FROM api_request WHERE endpoint='/v4/fixtures'").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="only OddsPapi /v4/historical-odds"):
        con.execute("INSERT INTO request_outcome VALUES (?, 'no_history', 404, 1)", (other,))


def test_history_not_due_before_delay(env, tmp_path):
    cfg, con, fake, f = env
    op = mapped_candidate(env, tmp_path)
    start = close.iso_ms("2026-10-10T16:30:00Z")
    assert op.history_due(start + 2 * H) == [] and op.history_due(start + 4 * H) == ["idA"]


# --- bookmaker check ------------------------------------------------------------------------------

def test_bookmaker_check():
    assert diag.bookmaker_check([(4, "Pinnacle"), (8, "Bet365")]).startswith("PASS")
    assert diag.bookmaker_check([(8, "Bet365")]).startswith("FAIL")
    assert diag.bookmaker_check([(4, "Pinnacle Sports")]).startswith("REVIEW")


def test_bookmaker_check_survives_null_name():          # the live crash of 2026-10-25
    names = [(4, "Pinnacle"), (99, None), (100, "")]
    assert diag.bookmaker_check(names).startswith("PASS")
    assert diag.unnamed_bookmakers(names) == [99, 100]


# --- mapping end to end -----------------------------------------------------------------------------

def test_mapping_end_to_end(env, tmp_path):
    cfg, con, fake, f = env
    ko = close.iso_ms("2026-10-10T16:30:00Z")
    seed_fixture(env, ko=ko)
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing()))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s))
    csv_path = tmp_path / "teams.csv"
    assert mapping.propose_teams(cfg, con, csv_path)["proposed_rows"] == 2
    assert mapping.apply(cfg, con) == {"team_not_allowlisted": 1}     # nothing maps before approval
    mapping.load_teams(con, csv_path, "c" * 40)
    assert mapping.apply(cfg, con) == {"mapped": 1}
    assert con.execute("SELECT fixture_id FROM provider_fixture_map").fetchone() == (1001,)
    assert mapping.apply(cfg, con) == {"already_mapped": 1}


def test_mapping_refuses_swapped_orientation(env, tmp_path):
    cfg, con, fake, f = env
    ko = close.iso_ms("2026-10-10T16:30:00Z")
    seed_fixture(env, ko=ko)
    con.execute("INSERT INTO provider_team_map VALUES ('oddspapi','17','Home FC',1,?),('oddspapi','44','Away FC',2,?)",
                ("c" * 40, "c" * 40))
    swapped = [dict(listing()[0], participant1Id=44, participant2Id=17,
                    participant1Name="Away FC", participant2Name="Home FC")]
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing(items=swapped)))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s))
    assert mapping.apply(cfg, con) == {"no_unique_af_fixture": 1}


def test_load_teams_refuses_conflicts(env, tmp_path):
    cfg, con, fake, f = env
    p = tmp_path / "t.csv"
    p.write_text("provider,provider_team_key,provider_team_name,team_id,af_team_name,supporting_fixtures,conflicts\n"
                 "oddspapi,17,Home FC,1,Home FC,3,Away FC(1)\n")
    with pytest.raises(ValueError, match="conflicts"):
        mapping.load_teams(con, p, "c" * 40)


def test_kickoff_audit_records_disagreement(env):
    cfg, con, fake, f = env
    ko = close.iso_ms("2026-10-10T16:30:00Z")
    seed_fixture(env, ko=ko)
    con.execute("INSERT INTO provider_team_map VALUES ('oddspapi','17','Home FC',1,?),('oddspapi','44','Away FC',2,?)",
                ("c" * 40, "c" * 40))
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing()))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s))
    mapping.apply(cfg, con)
    # api-football later reschedules by a day and reports FT; the old mapping must be flagged
    fake.routes.clear()
    fake.add("/fixtures", {"league": 39, "season": 2026}, (200, af_page([af_fixture(1001, ko + 24 * H, status="FT")])))
    run(con, "af_fixtures", lambda s: AF(cfg, con, f).fixtures(s))
    assert mapping.audit_kickoffs(con) == {"disagree": 1}
    assert con.execute("SELECT checked, disagreements FROM v_misjoin_rate").fetchone() == (1, 1)


# --- ledger -------------------------------------------------------------------------------------------

def test_plan_and_watchdog(env):
    cfg, con, fake, f = env
    t = now_ms()
    assert ledger.plan(con, cfg.active_jobs, horizon_h=48, t=t)["slots_planned"] > 0
    assert ledger.watchdog(con, cfg.active_jobs, t=t) == []
    later = t + 3 * H
    missed = ledger.watchdog(con, cfg.active_jobs, t=later)
    assert any(j == "af_odds" for j, _ in missed)


def test_watchdog_reports_absent_plan(env):              # ABSENT: planner never ran
    cfg, con, fake, f = env
    missed = ledger.watchdog(con, cfg.active_jobs)
    assert {m[0] for m in missed} == {f"{j}:no_future_slot_planned" for j in ledger.CADENCE}


def test_completed_slot_cannot_rerun(env):
    cfg, con, fake, f = env
    t = now_ms()
    run_at = ledger.nominal("af_odds", t)
    with ledger.run_slot(con, "af_odds", scheduled_for=run_at):
        pass
    with pytest.raises(RuntimeError, match="already complete"):
        with ledger.run_slot(con, "af_odds", scheduled_for=run_at):
            pass


def test_timers_match_ledger_cadence():
    """Each systemd timer's OnCalendar must agree with ledger.CADENCE, or the watchdog lies."""
    import re
    deploy = ROOT / "deploy"
    for job, (period, offset) in ledger.CADENCE.items():
        unit = (deploy / f"corners-{job.replace('_', '-')}.timer").read_text()
        cal = re.search(r"OnCalendar=(.+)", unit).group(1).strip()
        minute = offset // 60_000
        if period == H:
            assert cal == f"*-*-* *:{minute:02d}:00", (job, cal)
        elif period == 6 * H:
            assert cal == f"*-*-* 00/6:{minute:02d}:00", (job, cal)
        elif period == 24 * H:
            assert cal == f"*-*-* 00:{minute:02d}:00", (job, cal)
        assert "Persistent=false" in unit and "RandomizedDelaySec=0" in unit


# --- backup and CLI ---------------------------------------------------------------------------------

def test_backup_restore_test_passes(env):
    from collector.backup import backup
    cfg, con, fake, f = env
    seed_fixture(env)
    r = backup(cfg.db_path, cfg.db_path.parent / "backups")
    assert r["restore_test"] == "ok" and r["rows"]["fixture"] == 1


def test_restore_test_detects_short_copy(env, tmp_path):   # ABSENT rows in the restored copy
    from collector.backup import backup, restore_test
    cfg, con, fake, f = env
    r = backup(cfg.db_path, cfg.db_path.parent / "backups")
    with pytest.raises(RuntimeError, match="fewer rows"):
        restore_test(Path(r["backup"]), {**r["rows"], "fixture": 5}, 7)


def test_cli_offline_commands(env, capsys):
    from collector.__main__ import main
    cfg = env[0]
    cfg_path = cfg.db_path.parent.parent / "config.toml"
    assert main(["--config", str(cfg_path), "plan"]) == 0
    assert main(["--config", str(cfg_path), "watchdog"]) == 0
    assert main(["--config", str(cfg_path), "report"]) == 0
    assert '"fixtures_tracked"' in capsys.readouterr().out


def test_cli_missing_key_fails_cleanly(env, monkeypatch, capsys):   # ABSENT secret
    from collector.__main__ import main
    monkeypatch.setenv("AF_KEY", "")
    cfg = env[0]
    rc = main(["--config", str(cfg.db_path.parent.parent / "config.toml"), "af-odds"])
    assert rc == 1 and "AF_KEY is not set" in capsys.readouterr().err
    assert env[1].execute("SELECT status FROM capture_slot WHERE job='af_odds'").fetchone() == ("failed",)


def patched_main(monkeypatch, fake):
    import collector.__main__ as m
    real = m.Fetcher
    monkeypatch.setattr(m, "Fetcher", lambda con, archive, secrets=(): real(con, archive, transport=fake,
                                                                           sleep=lambda s: None, secrets=secrets))
    return m.main


def test_cli_day1_check_fixture(env, monkeypatch, capsys):
    cfg, con, fake, f = env
    rec = lambda t, p, a=True: {"createdAt": t, "price": p, "limit": 1, "active": a}  # noqa: E731
    body = {"fixtureId": "id1000001772221292", "bookmakers": {"pinnacle": {"markets": {
        "101": {"outcomes": {"101": {"players": {"0": [rec("2026-10-10T11:20:00Z", 2.0),
                                                        rec("2026-10-10T11:29:00Z", 2.05),     # inside the 2-min buffer
                                                        rec("2026-10-10T11:40:00Z", 6.00)]}}}}}}}}  # in-play
    fake.add("/v4/historical-odds", {"fixtureId": "id1000001772221292", "bookmakers": "pinnacle"}, (200, body))
    main = patched_main(monkeypatch, fake)
    rc = main(["--config", str(cfg.db_path.parent.parent / "config.toml"), "op-check-fixture",
               "id1000001772221292", "--kickoff", "2026-10-10T11:30:00Z"])
    out_ = json.loads(capsys.readouterr().out)
    assert rc == 0 and out_["rule"] == "close-v1"
    assert out_["series_with_close"] == 1
    assert out_["records_in_buffer"] == 1 and out_["records_at_or_after_cutoff"] == 2
    assert out_["close_age_last_record_min"]["median"] == 8.0      # 11:28 cutoff - 11:20 record
    assert (cfg.review_dir / "check_id1000001772221292.json").exists()


def test_cli_bootstrap_catalogs_unnamed_books(env, monkeypatch, capsys):
    cfg, con, fake, f = env
    fake.add("/status", {}, (200, af_page({"subscription": {"plan": "Pro"}})))
    fake.add("/odds/bookmakers", {}, (200, af_page([{"id": 4, "name": "Pinnacle"}, {"id": 99, "name": None}])))
    fake.add("/odds/bets", {}, (200, af_page([{"id": 1, "name": "Match Winner"}])))
    main = patched_main(monkeypatch, fake)
    rc = main(["--config", str(cfg.db_path.parent.parent / "config.toml"), "af-bootstrap"])
    out_ = json.loads(capsys.readouterr().out)
    assert rc == 0 and out_["unnamed_bookmaker_ids"] == [99]
    assert con.execute("SELECT provider_book_name FROM provider_book_catalog WHERE provider_book_key='99'").fetchone() == (None,)
    con.execute("INSERT INTO bookmaker VALUES (99, 'Unknown 99', 'observed')")
    with pytest.raises(sqlite3.IntegrityError, match="latest catalog name is empty"):
        con.execute("INSERT INTO provider_bookmaker_map VALUES ('api_football', '99', 99, ?)", ("c" * 40,))


def test_cli_bootstrap_reports_pinnacle(env, monkeypatch, capsys):
    cfg, con, fake, f = env
    fake.add("/status", {}, (200, af_page({"subscription": {"plan": "Pro"}})))
    fake.add("/odds/bookmakers", {}, (200, af_page([{"id": 4, "name": "Pinnacle"}, {"id": 8, "name": "Bet365"}])))
    fake.add("/odds/bets", {}, (200, af_page([{"id": 1, "name": "Match Winner"}])))
    main = patched_main(monkeypatch, fake)
    rc = main(["--config", str(cfg.db_path.parent.parent / "config.toml"), "af-bootstrap"])
    out_ = json.loads(capsys.readouterr().out)
    assert rc == 0 and out_["pinnacle_check"].startswith("PASS")


def test_backfill_window_reaches_old_fixtures(env):
    cfg, con, fake, f = env
    ko = (now_ms() - 5 * 24 * H) // 1000 * 1000
    seed_fixture(env, ko=ko)
    fake.add("/fixtures", {"ids": "1001"}, (200, af_page([af_fixture(1001, ko, status="FT", ft=(0, 0))])))
    fake.add("/fixtures/statistics", {"fixture": 1001}, (200, af_page([
        {"team": {"id": 1}, "statistics": [{"type": "Corner Kicks", "value": 4}]},
        {"team": {"id": 2}, "statistics": [{"type": "Corner Kicks", "value": 6}]}])))
    hourly = run(con, "af_results", lambda s: AF(cfg, con, f).results(s))
    assert hourly["checked"] == 0                       # outside the hourly window
    t = now_ms()
    back = run(con, "af_results", lambda s: AF(cfg, con, f).results(s, since_ms=t - 400 * 86_400_000,
                                                                       until_ms=t - 72 * H))
    assert back["outcomes_recorded"] == 1


# --- fix 5: tracked fixtures with no mapping -----------------------------------------------------

def upcoming_setup(env, n=6, listed=0, tid_items=None):
    cfg, con, fake, f = env
    base = (now_ms() + 24 * H) // 1000 * 1000
    fx = [af_fixture(2000 + i, base + i * H, home=(10 + i, f"H{i}"), away=(50 + i, f"A{i}")) for i in range(n)]
    fake.add("/fixtures", {"league": 39, "season": 2026}, (200, af_page(fx)))
    run(con, "af_fixtures", lambda s: AF(cfg, con, f).fixtures(s))
    items = [{"fixtureId": f"id{i}", "participant1Id": 100 + i, "participant2Id": 500 + i,
              "participant1Name": f"H{i}", "participant2Name": f"A{i}",
              "startTime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime((base + i * H) / 1000)),
              "statusId": 0, "tournamentId": 17} for i in range(listed)]
    return cfg, con, fake, f, items


def test_unmapped_share_alerts_and_distinguishes_causes(env):
    cfg, con, fake, f, items = upcoming_setup(env, n=6, listed=3)
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, items))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s, force=[17]))
    um = mapping.unmapped_upcoming(cfg, con)["17"]
    assert um == {"upcoming": 6, "mapped": 0, "candidate_unmapped": 3, "no_candidate": 3, "unmapped_share": 1.0,
                  "alert": True, "needs_relisting": True}


def test_cli_op_fixtures_relists_when_candidates_absent_and_alerts(env, monkeypatch, capsys):
    cfg, con, fake, f, items = upcoming_setup(env, n=6, listed=0)
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, []))
    main = patched_main(monkeypatch, fake)
    rc = main(["--config", str(cfg.db_path.parent.parent / "config.toml"), "op-fixtures"])
    res = json.loads(capsys.readouterr().out)
    assert res["due"] == [17] and rc == 4 and res["alert"] == ["17"]   # listed once (never listed before), still unmapped


def test_no_alert_when_nothing_upcoming(env):                       # ABSENT fixtures: no false alarm
    cfg, con, fake, f = env
    assert mapping.unmapped_upcoming(cfg, con)["17"]["alert"] is False


def test_load_teams_reads_committed_bytes_not_working_copy(env, tmp_path):
    import subprocess
    cfg, con, fake, f = env
    repo = tmp_path / "repo"
    (repo / "review").mkdir(parents=True)
    g = ["git", "-C", str(repo)]
    subprocess.run(g + ["init", "-q"], check=True)
    csv_text = ("provider,provider_team_key,provider_team_name,team_id,af_team_name,supporting_fixtures,conflicts\n"
                "oddspapi,17,Home FC,1,Home FC,3,\n")
    (repo / "review" / "t.csv").write_text(csv_text)
    con.execute("INSERT INTO team VALUES (1,'Home FC'),(2,'Away FC')")
    subprocess.run(g + ["add", "-A"], check=True)
    subprocess.run(g + ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "map"], check=True)
    sha = subprocess.run(g + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    (repo / "review" / "t.csv").write_text(csv_text + "oddspapi,44,Away FC,2,Away FC,3,\n")   # uncommitted edit
    assert mapping.load_teams_from_git(con, repo, sha, "review/t.csv")["loaded"] == 1
    assert con.execute("SELECT approved_in_commit FROM provider_team_map").fetchall() == [(sha,)]
    with pytest.raises(ValueError):
        mapping.load_teams_from_git(con, repo, sha[:12], "review/t.csv")
    with pytest.raises(subprocess.CalledProcessError):
        mapping.load_teams_from_git(con, repo, "f" * 40, "review/t.csv")


# --- v0.5.2: active jobs, watchdog naming, timer drift ----------------------------------------------

ONLY_AF = ("af_fixtures", "af_odds", "af_results")


def test_inactive_job_gets_no_slots_and_no_watchdog_failure(env):
    cfg, con, fake, f = env
    t = now_ms()
    r = ledger.plan(con, ONLY_AF, horizon_h=48, t=t)
    assert r["slots_planned"] > 0 and r["active_jobs"] == list(ONLY_AF)
    jobs = {j for (j,) in con.execute("SELECT DISTINCT job FROM capture_slot")}
    assert jobs == set(ONLY_AF)
    for later in (t + 3 * H, t + 30 * H):                       # no op_* misses, ever
        assert all(not m[0].startswith("op_") for m in ledger.watchdog(con, ONLY_AF, t=later))
    assert ledger.watchdog(con, ONLY_AF, t=t) == []


def test_plan_removes_pending_slots_of_jobs_made_inactive(env):    # the 25-26 Oct VM state
    cfg, con, fake, f = env
    t = now_ms()
    ledger.plan(con, ledger.CADENCE, horizon_h=48, t=t)          # old planner: every job
    with ledger.run_slot(con, "op_history", scheduled_for=ledger.nominal("op_history", t)):
        pass                                                     # one op_history slot actually ran
    r = ledger.plan(con, ONLY_AF, horizon_h=48, t=t)
    assert set(r["pending_removed_for_inactive_jobs"]) == {"op_account", "op_fixtures", "op_history"}
    left = con.execute("SELECT job, status FROM capture_slot WHERE job LIKE 'op_%'").fetchall()
    assert left == [("op_history", "complete")]                  # history of what ran is kept
    assert ledger.watchdog(con, ONLY_AF, t=t + 30 * H) and \
        all(not m[0].startswith("op_") for m in ledger.watchdog(con, ONLY_AF, t=t + 31 * H))


def test_scheduled_jobs_equal_ledger_cadence():
    from collector.config import SCHEDULED_JOBS
    assert set(SCHEDULED_JOBS) == set(ledger.CADENCE)


def test_config_requires_valid_active_jobs(tmp_path):
    base = 'db_path = "c.db"\n[[leagues]]\nleague_id = 39\nseason = 2026\n'
    (tmp_path / "a.toml").write_text(base)
    with pytest.raises(ValueError, match="active_jobs is missing"):
        load(tmp_path / "a.toml")
    (tmp_path / "b.toml").write_text('active_jobs = ["af_odds", "op_bogus"]\n' + base)
    with pytest.raises(ValueError, match="unknown or repeated"):
        load(tmp_path / "b.toml")


def test_production_config_active_jobs():
    cfg = load(ROOT / "config" / "production.toml")
    assert "af_odds" in cfg.active_jobs
    assert set(cfg.active_jobs) <= set(ledger.CADENCE)


def cli(env):
    return ["--config", str(env[0].db_path.parent.parent / "config.toml")]


def set_active(env, jobs):
    p = env[0].db_path.parent.parent / "config.toml"
    import re
    p.write_text(re.sub(r"active_jobs = \[.*\]", "active_jobs = [" + ", ".join(f'"{j}"' for j in jobs) + "]",
                        p.read_text()))


def test_cli_plan_and_watchdog_ignore_inactive_jobs(env, capsys):
    from collector.__main__ import main
    set_active(env, ONLY_AF)
    assert main(cli(env) + ["plan"]) == 0
    assert main(cli(env) + ["watchdog"]) == 0
    out_ = capsys.readouterr().out
    assert '"summary": "ok"' in out_ and '"op_' not in out_.split('"active_jobs"')[1].split("]")[0]


def test_watchdog_failure_names_the_missed_jobs_in_the_ping(env, monkeypatch, capsys):
    import collector.__main__ as m
    cfg, con, fake, f = env
    monkeypatch.setenv("HC_WATCHDOG", "https://hc.example/ping/abc")
    monkeypatch.setenv("CORNERS_TRIGGER", "timer")
    sent = []
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda req, timeout=10: sent.append((req.full_url, req.data)) or type(
                            "R", (), {"read": lambda self: b""})())
    t = now_ms() - 3 * H
    ledger.plan(con, cfg.active_jobs, horizon_h=48, t=t)        # planned 3 h ago, nothing ran since
    assert m.main(cli(env) + ["watchdog"]) == 1
    url, body = sent[-1]
    assert url.endswith("/fail") and re.search(rb"af_odds: [23] \(missed @", body)
    assert "watchdog FAIL: " in capsys.readouterr().err


def test_timer_run_of_inactive_job_does_nothing_and_alerts(env, monkeypatch, capsys):
    from collector.__main__ import main
    cfg, con, fake, f = env
    set_active(env, ONLY_AF)
    monkeypatch.setenv("CORNERS_TRIGGER", "timer")
    assert main(cli(env) + ["op-account"]) == 5
    assert fake.calls == []                                       # no API call at all
    assert "not in active_jobs" in capsys.readouterr().err
    assert con.execute("SELECT status FROM capture_slot WHERE job='op_account'").fetchone() == ("failed",)
    monkeypatch.delenv("CORNERS_TRIGGER")
    main(cli(env) + ["plan"])
    rc = main(cli(env) + ["watchdog"])
    assert rc == 1 and "timer_enabled_but_not_in_active_jobs" in capsys.readouterr().out


def test_by_hand_run_of_inactive_job_uses_an_unplanned_slot(env, monkeypatch, capsys):
    main = patched_main(monkeypatch, env[2])
    cfg, con, fake, f = env
    set_active(env, ONLY_AF)
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 3}))
    assert main(cli(env) + ["op-account"]) == 0
    row = con.execute("SELECT status, note, scheduled_for_ms FROM capture_slot WHERE job='op_account'").fetchone()
    assert row[0] == "complete" and row[1] != ledger.INACTIVE_TIMER_NOTE
    assert row[2] != ledger.nominal("op_account", row[2])            # its own unplanned slot, not the plan's


def test_failure_ping_never_carries_a_key(env, monkeypatch, capsys):
    import collector.__main__ as m
    cfg, con, fake, f = env
    monkeypatch.setenv("HC_AF_ODDS", "https://hc.example/ping/odds")
    monkeypatch.setenv("CORNERS_TRIGGER", "timer")
    sent = []
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda req, timeout=10: sent.append(req.data) or type("R", (), {"read": lambda s: b""})())
    monkeypatch.setattr(m, "dispatch", lambda *a: (_ for _ in ()).throw(RuntimeError(f"GET ?apiKey={AF_KEY} 500")))
    assert m.main(cli(env) + ["af-odds"]) == 1
    assert sent and all(AF_KEY.encode() not in (b or b"") for b in sent)


# --- v0.5.2: backup status and off-box ---------------------------------------------------------------

def test_backup_status_and_report_line(env, monkeypatch):
    from collector import backup as b
    cfg, con, fake, f = env
    monkeypatch.delenv("RCLONE_REMOTE", raising=False)
    assert b.main(["--config", str(cfg.db_path.parent.parent / "config.toml")]) == 0
    st = b.status(cfg.db_path.parent)
    assert st["problems"] == [] and st["local"]["last_attempt"]["result"] == "ok"
    assert st["offbox"]["configured"] is False
    from collector.__main__ import report
    rep = report(con, cfg)
    assert rep["backup"]["local"]["newest_file"].startswith("corners-")
    assert set(rep["storage"]) >= {"archive_mb_last_24h", "archive_projected_gb_per_30d", "disk_free_gb"}


def test_backup_stale_and_offbox_never_synced_are_problems(env, monkeypatch):
    import os
    from collector import backup as b
    cfg, con, fake, f = env
    r = b.backup(cfg.db_path, cfg.db_path.parent / "backups")
    old = time.time() - 30 * 3600
    os.utime(r["backup"], (old, old))
    st = b.status(cfg.db_path.parent, offbox_configured=True, first_data_s=old - 3600)
    assert "backup:no_backup_in_26h" in st["problems"] and "offbox:configured_but_never_synced" in st["problems"]


def test_no_backup_yet_on_a_fresh_install_is_not_a_problem(env):
    from collector import backup as b
    cfg = env[0]
    assert b.status(cfg.db_path.parent, offbox_configured=False, first_data_s=time.time() - 600)["problems"] == []


def test_offbox_incomplete_copy_is_recorded_as_failed(env, monkeypatch, capsys):
    from collector import backup as b
    cfg, con, fake, f = env
    run(con, "af_fixtures", lambda s: AF(cfg, con, f).call(s, "/leagues", {"id": 39, "season": 2026}, "x"))
    monkeypatch.setenv("RCLONE_REMOTE", "corners-offbox:corners")
    args = ["--config", str(cfg.db_path.parent.parent / "config.toml"), "--offbox-result", "ok",
            "--copy-started", str(time.time() + 5)]
    assert b.main(args + ["--remote-archive-files", "0"]) == 1          # remote is missing a file
    st = b.status(cfg.db_path.parent, offbox_configured=True)
    assert "offbox:last_sync_failed" in st["problems"]
    assert b.main(args + ["--remote-archive-files", "1"]) == 0
    assert b.status(cfg.db_path.parent, offbox_configured=True)["offbox"]["last_sync"]["result"] == "ok"


def test_backup_check_file(env, capsys):
    from collector import backup as b
    cfg = env[0]
    r = b.backup(cfg.db_path, cfg.db_path.parent / "backups")
    assert b.main(["--check-file", r["backup"]]) == 0
    assert '"integrity_check": "ok"' in capsys.readouterr().out


def test_cli_close_report_reads_the_archive_without_calling_the_api(env, monkeypatch, capsys):
    cfg, con, fake, f = env
    rec = lambda t, p, a=True: {"createdAt": t, "price": p, "limit": 1, "active": a}  # noqa: E731
    body = {"fixtureId": "idX", "bookmakers": {"pinnacle": {"markets": {
        "101": {"outcomes": {"101": {"players": {"0": [rec("2026-10-10T11:20:00Z", 2.0)]}},
                             "102": {"players": {"0": [rec("2026-10-10T11:20:00Z", 3.0),
                                                       rec("2026-10-10T11:27:00Z", 3.0, False)]}}}}}}}}
    fake.add("/v4/historical-odds", {"fixtureId": "idX", "bookmakers": "pinnacle"}, (200, body))
    main = patched_main(monkeypatch, fake)
    main(cli(env) + ["op-check-fixture", "idX", "--kickoff", "2026-10-10T11:30:00Z"])
    capsys.readouterr()
    n_calls = len(fake.calls)
    assert main(cli(env) + ["close-report", "idX", "--kickoff", "2026-10-10T11:30:00Z"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert len(fake.calls) == n_calls                              # archive only
    assert rep["by_market"]["101"] == {"series": 2, "with_close_v1": 1, "suspended_at_cutoff": 1,
                                       "with_close_v1b": 1}
    assert rep["suspended_at_cutoff_detail"][0]["suspended_for_min"] == 1.0
    assert rep["start_sources_present"] == 1 and rep["counts_toward_g05a"] is False


# --- v0.5.3: by-hand runs never touch the plan or healthchecks ---------------------------------------

def hc_spy(monkeypatch):
    import collector.__main__ as m
    sent = []
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda req, timeout=10: sent.append(req.full_url) or type("R", (), {"read": lambda s: b""})())
    return sent


def test_by_hand_run_of_active_job_after_its_slot_completed(env, monkeypatch, capsys):   # the 26 Oct bug
    cfg, con, fake, f = env
    monkeypatch.setenv("HC_AF_FIXTURES", "https://hc.example/ping/fx")
    sent = hc_spy(monkeypatch)
    main = patched_main(monkeypatch, fake)
    fake.add("/fixtures", {"league": 39, "season": 2026}, (200, af_page([])))
    monkeypatch.setenv("CORNERS_TRIGGER", "timer")
    assert main(cli(env) + ["af-fixtures"]) == 0                       # the timer run completes its slot
    planned = con.execute("SELECT slot_id, status, note FROM capture_slot WHERE job='af_fixtures'").fetchall()
    assert [r[1] for r in planned] == ["complete"] and sent            # and pings
    monkeypatch.delenv("CORNERS_TRIGGER")
    sent.clear()
    assert main(cli(env) + ["af-fixtures"]) == 0                       # by hand, same period: no collision
    rows = con.execute("SELECT status, note FROM capture_slot WHERE job='af_fixtures' ORDER BY slot_id").fetchall()
    assert rows == [("complete", "unplanned: no pending slot existed"), ("complete", ledger.BY_HAND_NOTE)]
    assert sent == []                                                  # no healthchecks ping at all


def test_by_hand_failure_does_not_ping_fail(env, monkeypatch, capsys):
    cfg, con, fake, f = env
    monkeypatch.setenv("HC_AF_ODDS", "https://hc.example/ping/odds")
    monkeypatch.setenv("AF_KEY", "")
    sent = hc_spy(monkeypatch)
    from collector.__main__ import main
    assert main(cli(env) + ["af-odds"]) == 1
    assert sent == [] and "AF_KEY is not set" in capsys.readouterr().err


def test_by_hand_run_never_claims_or_hides_a_missed_slot(env):
    cfg, con, fake, f = env
    t = now_ms()
    ledger.plan(con, cfg.active_jobs, horizon_h=48, t=t - 3 * H)
    missed = ledger.watchdog(con, cfg.active_jobs, t=t)
    assert any(j == "af_odds" for j, _ in missed)
    before = con.execute("SELECT count(*) FROM capture_slot WHERE job='af_odds' AND status='missed'").fetchone()
    with ledger.run_slot(con, "af_odds", by_hand=True):
        pass
    assert con.execute("SELECT count(*) FROM capture_slot WHERE job='af_odds' AND status='missed'").fetchone() == before


def test_by_hand_slot_never_lands_on_a_planned_timestamp(env, monkeypatch):
    cfg, con, fake, f = env
    t = now_ms()
    s_for = ledger.nominal("af_odds", t) + H
    con.execute("INSERT INTO capture_slot (job, scheduled_for_ms) VALUES ('af_odds', ?)", (s_for,))
    monkeypatch.setattr(ledger, "now_ms", lambda: s_for)
    with ledger.run_slot(con, "af_odds", by_hand=True) as slot:
        pass
    assert con.execute("SELECT status FROM capture_slot WHERE scheduled_for_ms=?", (s_for,)).fetchone() == ("pending",)
    assert con.execute("SELECT scheduled_for_ms FROM capture_slot WHERE slot_id=?", (slot.slot_id,)).fetchone() == (s_for + 1,)


def test_report_explains_raw_rows_versus_requests(env):
    from collector.__main__ import report
    cfg, con, fake, f = env
    # one request with 2 pages (an extra page), one request retried once after a 500
    fake.add("/odds", {"league": 39, "season": 2026, "date": "2026-10-10"},
             (200, af_page([{"fixture": {"id": 1}}], 1, 2)))
    fake.add("/odds", {"league": 39, "season": 2026, "date": "2026-10-10", "page": 2},
             (200, af_page([{"fixture": {"id": 2}}], 2, 2)))
    run(con, "af_odds", lambda s: AF(cfg, con, f).call(s, "/odds", {"league": 39, "season": 2026, "date": "2026-10-10"}, "x"))
    fake.add("/fixtures", {"league": 39, "season": 2026}, (500, {"message": "boom"}), (200, af_page([])))
    run(con, "af_fixtures", lambda s: AF(cfg, con, f).call(s, "/fixtures", {"league": 39, "season": 2026}, "x"))
    r = report(con, cfg)["raw_vs_requests"]
    assert r["requests"] == 2 and r["raw_rows"] == 4
    assert r["extra_pages"] == 1 and r["retries"] == 1 and r["requests_without_raw"] == 0
    assert r["raw_rows"] == r["requests"] + r["extra_pages"] + r["retries"] - r["requests_without_raw"]
    assert r["retries_by_endpoint"] == {"api_football /fixtures": 1}


# --- v0.5.4: history probe, coverage and alert ------------------------------------------------------

def test_history_probe_one_fixture_per_month_with_control(env, monkeypatch, capsys):
    cfg, con, fake, f = env
    items = [dict(listing()[0], fixtureId=f"m{m}", startTime=f"2026-{m:02d}-16T15:00:00Z") for m in (1, 2, 3)]
    items.append(dict(listing()[0], fixtureId="early1", startTime="2026-01-03T15:00:00Z"))
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing(items=items)))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s))
    for fid, st in (("m1", 404), ("m2", 404), ("m3", 200), ("ctl", 200)):
        fake.add("/v4/historical-odds", {"fixtureId": fid, "bookmakers": "pinnacle"},
                 (st, {"fixtureId": fid, "bookmakers": {}} if st == 200 else {"message": "nf"}))
    main = patched_main(monkeypatch, fake)
    rc = main(cli(env) + ["op-history-probe", "--tournaments", "17", "--months", "1-4", "--control", "ctl"])
    out_ = json.loads(capsys.readouterr().out)
    assert rc == 0 and out_["control"]["result"] == "usable"
    assert out_["months"]["2026-01"][0]["fixture"] == "m1"            # nearest after the 15th, not early1
    assert [out_["months"][k][0]["http_status"] for k in ("2026-01", "2026-02", "2026-03")] == [404, 404, 200]
    assert out_["months"]["2026-04"].startswith("no finished candidate")
    assert con.execute("SELECT status, note FROM capture_slot WHERE job='op_history'").fetchone() == ("complete", "by hand")


def test_watchdog_alerts_when_forward_fixtures_have_no_history(env, monkeypatch, capsys):
    from collector.__main__ import history_health, main
    cfg, con, fake, f = env
    recent = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2 * 86400))
    items = [dict(listing()[0], fixtureId=f"r{i}", startTime=recent) for i in range(12)]
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing(items=items)))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s))
    for i in range(12):
        fake.add("/v4/historical-odds", {"fixtureId": f"r{i}", "bookmakers": "pinnacle"}, (404, {}))
    run(con, "op_history", lambda s: OP(cfg, con, f).history(s, ids=[f"r{i}" for i in range(12)]))
    h = history_health(con, now_ms())
    assert h["forward_requests_24h"] == 12 and h["forward_no_history_share_24h"] == 1.0
    main(cli(env) + ["plan"])
    assert main(cli(env) + ["watchdog"]) == 1
    assert "no_history on 100% of 12 requests" in capsys.readouterr().out


def test_uefa_qualifiers_do_not_count_but_the_round_of_32_does(env):
    from collector import eligibility
    cfg, con, fake, f = env
    con.execute("INSERT INTO league VALUES (2, 2026, 'UEFA Champions League', 'World', 'Cup')")
    d = lambda s: close.iso_ms(s)  # noqa: E731
    fx = [af_fixture(1, d("2026-08-19T19:00:00Z"), league=2, round_="Play-offs", home=(1, "A"), away=(2, "B")),
          af_fixture(2, d("2026-09-16T19:00:00Z"), league=2, round_="League Stage - 1", home=(3, "C"), away=(4, "D")),
          af_fixture(3, d("2027-02-16T20:00:00Z"), league=2, round_="Round of 32", home=(5, "E"),
                     away=(6, "F"))]   # api-football's real names (W7, 30 Oct)
    fake.add("/fixtures", {"league": 2, "season": 2026}, (200, af_page(fx)))
    fake.add("/leagues", {"id": 2, "season": 2026}, (200, af_page([{"league": {"id": 2, "name": "UCL", "type": "Cup"},
                                                                      "country": {"name": "World"}}])))
    import dataclasses
    from collector.config import League
    c2 = dataclasses.replace(cfg, leagues=(League(2, 2026),))
    run(con, "af_fixtures", lambda s: AF(c2, con, f).fixtures(s))
    assert eligibility.fixture_counts(con, 1) == (False, "qualifying round (before the league stage)")
    assert eligibility.fixture_counts(con, 2)[0] and eligibility.fixture_counts(con, 3)[0]


def test_probe_output_is_printed_even_if_the_slot_fails(env, monkeypatch, capsys):
    cfg, con, fake, f = env
    fake.add("/v4/historical-odds", {"fixtureId": "ctl", "bookmakers": "pinnacle"}, (429, {}))
    main = patched_main(monkeypatch, fake)
    rc = main(cli(env) + ["op-history-probe", "--months", "1", "--control", "ctl"])
    cap = capsys.readouterr()
    assert rc == 1 and '"rate_limited"' in cap.out and "SlotIncomplete" in cap.err


def test_op_backfill_honours_max_beyond_the_hourly_cap(env, tmp_path, monkeypatch):
    import dataclasses
    cfg, con, fake, f = env
    op = mapped_candidate(env, tmp_path)
    op.cfg = dataclasses.replace(cfg, op_history_max_per_run=0)       # the scheduled cap would take none
    assert op.history_due(now_ms()) == [] and op.history_due(now_ms(), limit=600) == ["idA"]


def test_g05a_history_status_rules():
    from collector.eligibility import g05a_history_status as g
    w = 1000
    assert g(2000, True, False, w) == "counted"
    assert g(2000, False, True, w) == "unusable"           # forward gap stays in the denominator
    assert g(500, False, True, w) == "out_of_window"        # before the first available day: excluded, reported
    assert g(2000, False, False, w) == "pending"


def test_history_window_start_is_measured(env, tmp_path):
    from collector.eligibility import history_window_start_ms
    cfg, con, fake, f = env
    op = mapped_candidate(env, tmp_path)
    assert history_window_start_ms(con) is None
    fake.add(*HIST, (200, {"fixtureId": "idA", "bookmakers": {}}))
    run(con, "op_history", lambda s: op.history(s))
    assert history_window_start_ms(con) == close.iso_ms("2026-10-10T16:30:00Z")


# --- v0.5.6: team-map proposals: placeholders, opponent propagation, decisions ----------------------

def ucl_world(env, tmp_path):
    """Domestic league 39 (tournament 17) plus UCL (league 2, tournament 7) with three
    league-stage fixtures at the SAME kickoff, so kickoff agreement alone can't map them."""
    import dataclasses
    from collector.config import League
    cfg, con, fake, f = env
    cfg2 = dataclasses.replace(cfg, leagues=(League(39, 2026, 17), League(2, 2026, 7)))
    ko_dom = close.iso_ms("2026-10-03T14:00:00Z")
    ko_ucl = close.iso_ms("2026-10-21T19:00:00Z")
    fake.add("/fixtures", {"league": 39, "season": 2026},
             (200, af_page([af_fixture(500, ko_dom, home=(10, "Arsenal"), away=(11, "Leeds"))])))
    fake.add("/leagues", {"id": 2, "season": 2026}, (200, af_page([{"league": {"id": 2, "name": "UCL", "type": "Cup"},
                                                                      "country": {"name": "World"}}])))
    fake.add("/fixtures", {"league": 2, "season": 2026}, (200, af_page([
        af_fixture(601, ko_ucl, league=2, round_="League Stage - 3", home=(10, "Arsenal"), away=(20, "PSV")),
        af_fixture(602, ko_ucl, league=2, round_="League Stage - 3", home=(21, "Porto"), away=(22, "Slavia")),
        af_fixture(603, ko_ucl, league=2, round_="League Stage - 3", home=(23, "Celtic"), away=(11, "Leeds"))])))
    run(con, "af_fixtures", lambda s: AF(cfg2, con, f).fixtures(s))
    c = lambda fid, t, hk, hn, ak, an, st: {"fixtureId": fid, "participant1Id": hk, "participant2Id": ak,  # noqa: E731
                                            "participant1Name": hn, "participant2Name": an, "startTime": st,
                                            "statusId": 0, "tournamentId": t}
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, [c("d1", 17, 42, "Arsenal FC", 34, "Leeds United",
                                                            "2026-10-03T14:00:00Z")]))
    fake.add("/v4/fixtures", {"tournamentId": 7}, (200, [
        c("u1", 7, 42, "Arsenal FC", 2952, "PSV Eindhoven", "2026-10-21T19:00:00Z"),
        c("u2", 7, 3002, "FC Porto", 2216, "Slavia Prague", "2026-10-21T19:00:00Z"),
        c("u3", 7, 2352, "Celtic Glasgow", 34, "Leeds United", "2026-10-21T19:00:00Z"),
        c("u4", 7, 1374138, "Winner Match 2", 2216, "Slavia Prague", "2026-08-20T19:00:00Z")]))
    run(con, "op_fixtures", lambda s: OP(cfg2, con, f).fixtures(s, force=[17, 7]))
    return cfg2


def read_rows(path):
    import csv as _csv
    return {r["provider_team_key"]: r for r in _csv.DictReader(open(path))}


def test_simultaneous_kickoffs_need_an_approved_opponent(env, tmp_path):
    cfg, con, fake, f = env
    cfg2 = ucl_world(env, tmp_path)
    p1 = tmp_path / "v1.csv"
    r1 = mapping.propose_teams(cfg2, con, p1)
    rows = read_rows(p1)
    assert set(rows) == {"42", "34"}                      # domestic only: UEFA kickoffs are ambiguous
    assert r1["via_opponent_rows"] == 0 and r1["placeholders_excluded"] == ["Winner Match 2"]
    assert mapping.load_teams(con, p1, "a" * 40) == {"loaded": 2, "already_loaded": 0}
    p2 = tmp_path / "v2.csv"
    r2 = mapping.propose_teams(cfg2, con, p2)
    rows = read_rows(p2)
    assert rows["2952"]["team_id"] == "20" and rows["2952"]["via_opponent"] == "1"      # PSV, pinned by Arsenal
    assert rows["2352"]["team_id"] == "23"                                              # Celtic, pinned by Leeds
    assert "3002" not in rows and "2216" not in rows                                    # no approved opponent
    assert "1374138" not in rows and r2["via_opponent_rows"] == 2
    assert mapping.load_teams(con, p2, "b" * 40)["loaded"] == 2
    assert mapping.apply(cfg2, con).get("mapped") == 3                                  # d1, u1, u3


def test_cumulative_csv_reloads_and_changes_are_refused(env, tmp_path):
    cfg, con, fake, f = env
    cfg2 = ucl_world(env, tmp_path)
    p1 = tmp_path / "v1.csv"
    mapping.propose_teams(cfg2, con, p1)
    mapping.load_teams(con, p1, "a" * 40)
    assert mapping.load_teams(con, p1, "a" * 40) == {"loaded": 0, "already_loaded": 2}
    bad = tmp_path / "bad.csv"
    bad.write_text(p1.read_text().replace(",42,Arsenal FC,10,", ",42,Arsenal FC,11,"))
    with pytest.raises(ValueError, match="would change an approved pair"):
        mapping.load_teams(con, bad, "c" * 40)


def test_loader_needs_accept_and_refuses_placeholders(env, tmp_path):
    cfg, con, fake, f = env
    head = "provider,provider_team_key,provider_team_name,team_id,af_team_name,supporting_fixtures,via_kickoff,via_opponent,conflicts,decision\n"
    p = tmp_path / "t.csv"
    p.write_text(head + "oddspapi,17,Home FC,1,Home FC,3,3,0,Away FC(1),REVIEW: conflicts not dominated (3 vs 1)\n")
    with pytest.raises(ValueError, match="resolve it to 'accept'"):
        mapping.load_teams(con, p, "c" * 40)
    p.write_text(head + "oddspapi,1374138,Winner Match 2,1,Home FC,1,1,0,,accept\n")
    with pytest.raises(ValueError, match="placeholder participant"):
        mapping.load_teams(con, p, "c" * 40)


@pytest.mark.parametrize("name,ph", [("Winner Match 2", True), ("Loser Match 7", True), ("TBD", True),
                                     ("Match 4", True), ("Winners FC", False), ("Bodoe/Glimt", False),
                                     ("Viking FK", False), ("Union Saint-Gilloise", False)])
def test_placeholder_names(name, ph):
    assert mapping.is_placeholder(name) is ph


@pytest.mark.parametrize("n_good,want", [(5, "accept"), (4, "REVIEW: conflicts not dominated (4 vs 1)")])
def test_decision_rules(env, tmp_path, n_good, want):
    """5 vs 1 conflicting support is dominated (accept); 4 vs 1 is not (REVIEW)."""
    cfg, con, fake, f = env

    def world(n_good):
        days = [f"2026-10-{d:02d}T15:00:00Z" for d in range(1, n_good + 3)]
        af = [af_fixture(1000 + i, close.iso_ms(days[i]), home=(1, "Home FC"), away=(100 + i, f"Opp{i}"))
              for i in range(n_good)]
        af.append(af_fixture(1100, close.iso_ms(days[n_good]), home=(9, "Other FC"), away=(200, "Opp X")))
        cands = [dict(listing()[0], fixtureId=f"c{i}", participant1Id=17, participant2Id=500 + i,
                      participant2Name=f"P{i}", startTime=days[i]) for i in range(n_good)]
        cands.append(dict(listing()[0], fixtureId="cx", participant1Id=17, participant2Id=600, participant2Name="PX",
                          startTime=days[n_good]))
        return af, cands

    af, cands = world(n_good)
    fake.add("/fixtures", {"league": 39, "season": 2026}, (200, af_page(af)))
    run(con, "af_fixtures", lambda s: AF(cfg, con, f).fixtures(s))
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing(items=cands)))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s, force=[17]))
    out = tmp_path / "p.csv"
    mapping.propose_teams(cfg, con, out)
    assert read_rows(out)["17"]["decision"] == want


def test_one_team_for_two_keys_is_review(env, tmp_path):
    cfg, con, fake, f = env
    seed_fixture(env, ko=close.iso_ms("2026-10-10T16:30:00Z"))
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing(items=[
        listing()[0], dict(listing()[0], fixtureId="idB", participant1Id=99, participant1Name="Home FC II")])))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s, force=[17]))
    out = tmp_path / "p.csv"
    mapping.propose_teams(cfg, con, out)
    rows = read_rows(out)
    assert rows["17"]["decision"].startswith("REVIEW: team also proposed for key 99")
    assert rows["99"]["decision"].startswith("REVIEW: team also proposed for key 17")
