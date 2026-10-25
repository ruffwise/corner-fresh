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

def test_history_due_then_done(env):
    cfg, con, fake, f = env
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing()))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s))
    op = OP(cfg, con, f)
    assert op.history_due(now_ms()) == ["idA"]
    fake.add("/v4/historical-odds", {"fixtureId": "idA", "bookmakers": "pinnacle"}, (200, {"fixtureId": "idA",
                                                                                         "bookmakers": {}}))
    run(con, "op_history", lambda s: op.history(s))
    assert op.history_due(now_ms()) == []


def test_history_failed_pull_stays_due(env):            # ABSENT history: must retry
    cfg, con, fake, f = env
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing()))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s))
    op = OP(cfg, con, f)
    fake.add("/v4/historical-odds", {"fixtureId": "idA", "bookmakers": "pinnacle"}, (503, {"x": 1}))
    with pytest.raises(ledger.SlotIncomplete):
        run(con, "op_history", lambda s: op.history(s))
    assert op.history_due(now_ms()) == ["idA"]


def test_history_not_due_before_delay(env):
    cfg, con, fake, f = env
    soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))   # kicked off 1 h ago
    fake.add("/v4/account", {}, (200, {"request_limit": 250, "request_count": 0}))
    fake.add("/v4/fixtures", {"tournamentId": 17}, (200, listing(items=[dict(listing()[0], startTime=soon)])))
    run(con, "op_fixtures", lambda s: OP(cfg, con, f).fixtures(s))
    assert OP(cfg, con, f).history_due(now_ms()) == []


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
    assert mapping.propose_teams(cfg, con, csv_path) == 2
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
    assert ledger.plan(con, horizon_h=48, t=t) > 0
    assert ledger.watchdog(con, t=t) == []
    later = t + 3 * H
    missed = ledger.watchdog(con, t=later)
    assert any(j == "af_odds" for j, _ in missed)


def test_watchdog_reports_absent_plan(env):              # ABSENT: planner never ran
    cfg, con, fake, f = env
    missed = ledger.watchdog(con)
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
        restore_test(Path(r["backup"]), {**r["rows"], "fixture": 5}, 5)


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
    assert mapping.load_teams_from_git(con, repo, sha, "review/t.csv") == 1
    assert con.execute("SELECT approved_in_commit FROM provider_team_map").fetchall() == [(sha,)]
    with pytest.raises(ValueError):
        mapping.load_teams_from_git(con, repo, sha[:12], "review/t.csv")
    with pytest.raises(subprocess.CalledProcessError):
        mapping.load_teams_from_git(con, repo, "f" * 40, "review/t.csv")
