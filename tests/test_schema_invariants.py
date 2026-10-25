"""Two-sided tests for every schema invariant (schema v0.4).

Rule: each guard ships with
  (a) a BAD test: the bad case must fire;
  (b) an ABSENT test where applicable: missing expected data must fire;
  (c) a GOOD test: valid data passes, so the guard is not firing on everything.
The RAW layer carries the inverse obligation: it must ACCEPT degraded
responses (flagging them), never refuse them.

Times are relative to the real clock, because forecasts and picks are
checked against the database clock (no backfilled forecasts).
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
from connect import ConnectionContractError, assert_schema_contract, connect  # noqa: E402

SHA = "a" * 64
COMMIT = "c" * 40
MIN = 60_000
NOW = int(time.time() * 1000)
T0 = NOW - 60 * MIN               # fixtures first seen 1 h ago
KO = NOW + 4 * 60 * MIN           # upcoming fixture kicks off in 4 h
REFRESH = NOW - 10 * MIN          # odds snapshot 10 min ago
KO_PAST = NOW - 3 * 24 * 60 * MIN  # finished fixture, 3 days ago
GOALS_FT = "GOALS_TOTAL_FT_OU"
AF_GOALS = "5|Goals Over/Under"
OP_GOALS = "1010"   # placeholder OddsPapi market id; real ids come from the day-1 dump


def fires(con, sql, params=(), match=None):
    with pytest.raises(sqlite3.IntegrityError) as exc:
        con.execute(sql, params)
    if match:
        assert match in str(exc.value), str(exc.value)


class World:
    def __init__(self):
        self.con = connect(":memory:", create=True)
        c = self.con
        c.execute("INSERT INTO preregistration VALUES (1,'p0',?, 'v0', ?, NULL, ?, ?)",
                  (COMMIT, SHA, T0 - 10 * MIN, T0 - 10 * MIN))
        c.executemany("INSERT INTO provider VALUES (?,?,?)", [
            ("api_football", 1, "api-football Pro"), ("oddspapi", 0, "OddsPapi free tier")])
        c.executemany("INSERT INTO endpoint_allowlist VALUES (?,?,?)", [
            ("api_football", "/fixtures", 1), ("api_football", "/odds", 1),
            ("oddspapi", "/v4/fixtures", 1), ("oddspapi", "/v4/historical-odds", 0),
            ("oddspapi", "/v4/account", 0)])
        c.execute("INSERT INTO league VALUES (39, 2026, 'Premier League', 'England', 'League')")
        c.execute("INSERT INTO team VALUES (1,'Home FC'),(2,'Away FC'),(3,'Third FC')")
        c.execute("INSERT INTO bookmaker VALUES (4,'Pinnacle','reference_primary'),(8,'Bet365','observed')")
        c.executemany("INSERT INTO provider_bookmaker_map VALUES (?,?,?,?)", [
            ("api_football", "4", 4, COMMIT), ("api_football", "8", 8, COMMIT),
            ("oddspapi", "pinnacle", 4, COMMIT)])
        c.executemany("INSERT INTO market_def VALUES (?,?,?,?,?)", [
            (GOALS_FT, "goals_total", "match", "FT", "ou_half"),
            ("GOALS_TOTAL_1H_OU", "goals_total", "match", "1H", "ou_half"),
            ("MATCH_RESULT_FT", "match_result", "match", "FT", "three_way"),
            ("CORNERS_TOTAL_FT_OU", "corners", "match", "FT", "ou_half")])
        c.executemany("INSERT INTO provider_market_allowlist VALUES (?,?,?,?)", [
            ("api_football", AF_GOALS, GOALS_FT, COMMIT),
            ("api_football", "6|Goals Over/Under First Half", "GOALS_TOTAL_1H_OU", COMMIT),
            ("api_football", "1|Match Winner", "MATCH_RESULT_FT", COMMIT),
            ("oddspapi", OP_GOALS, GOALS_FT, COMMIT)])
        rows = [("api_football", GOALS_FT, "Over 2.5", "over", 10, COMMIT),
                ("api_football", GOALS_FT, "Under 2.5", "under", 10, COMMIT),
                ("api_football", "GOALS_TOTAL_1H_OU", "Over 2.5", "over", 10, COMMIT),
                ("api_football", "MATCH_RESULT_FT", "Home", "home", 0, COMMIT),
                ("api_football", "MATCH_RESULT_FT", "Draw", "draw", 0, COMMIT),
                ("api_football", "MATCH_RESULT_FT", "Away", "away", 0, COMMIT),
                ("oddspapi", GOALS_FT, "1010", "over", 10, COMMIT),
                ("oddspapi", GOALS_FT, "1011", "under", 10, COMMIT)]
        c.executemany("INSERT INTO value_allowlist VALUES (?,?,?,?,?,?)", rows)
        c.executemany("INSERT INTO provider_team_map VALUES ('oddspapi', ?, ?, ?, ?)", [
            ("17", "Home FC", 1, COMMIT), ("44", "Away FC", 2, COMMIT)])

        self.slot_fx = self.slot("af_fixtures", T0)
        self.req_fx = self.request(self.slot_fx, "/fixtures", "date=today")
        self.raw_fx = self.raw(self.req_fx, 1, 1, T0)
        self.add_fixture(1001, KO)
        self.slot_od = self.slot("af_odds", REFRESH)
        self.req_od = self.request(self.slot_od, "/odds", "fixture=1001")
        self.raw_od = self.raw(self.req_od, 1, 1, REFRESH)
        c.execute("INSERT INTO parse_run (parse_run_id, provider, kind, parser_git_commit, started_at_ms)"
                  " VALUES (1, 'api_football', 'snapshot', ?, ?)", (COMMIT, REFRESH + MIN))

    # --- builders -----------------------------------------------------------------
    def add_fixture(self, fid, kickoff, et_possible=0, home=1, away=2):
        self.con.execute("INSERT INTO fixture VALUES (?, 39, 2026, 'Regular Season - 8', ?, ?, ?, ?, ?)",
                         (fid, home, away, self.raw_fx, et_possible, "cup knockout" if et_possible else "league format"))
        self.con.execute("INSERT INTO fixture_state_obs (fixture_id, raw_id, observed_at_ms, kickoff_ms, status_short)"
                         " VALUES (?, ?, ?, ?, 'NS')", (fid, self.raw_fx, T0, kickoff))

    def slot(self, job, at):
        return self.con.execute(
            "INSERT INTO capture_slot (job, scheduled_for_ms, status, started_at_ms) VALUES (?,?,'running',?)",
            (job, at, at)).lastrowid

    def request(self, slot, endpoint, params, expected_pages=None, provider="api_football"):
        return self.con.execute(
            "INSERT INTO api_request (slot_id, provider, endpoint, params_canonical, expected_pages) VALUES (?,?,?,?,?)",
            (slot, provider, endpoint, params, expected_pages)).lastrowid

    def raw(self, req, page, total, at, skew=0, status=200, errors="[]", attempt=1, latency=500,
            date="auto", paged=1):
        server_date = (at + skew) if date == "auto" else date
        return self.con.execute(
            "INSERT INTO raw_response (request_id, page, attempt, paged, sha256, archive_path, http_status,"
            " requested_at_ms, received_at_ms, server_date_ms, body_bytes, api_errors_json, results_count,"
            " paging_current, paging_total) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (req, page, attempt, paged, SHA, "archive/aa/x.json.gz", status, at - latency, at, server_date, 100,
             errors, 1, page if (paged and total is not None) else None, total if paged else None)).lastrowid

    def quote(self, selection="over", value="Over 2.5", price=1.90, label=AF_GOALS, market=GOALS_FT,
              captured=None, kickoff=KO, raw=None, line_q=10, provider="api_football", book=4, run=1):
        raw = raw or self.raw_od
        if captured is None:
            captured = self.con.execute("SELECT received_at_ms FROM raw_response WHERE raw_id=?", (raw,)).fetchone()[0]
        return self.con.execute(
            "INSERT INTO quote (parse_run_id, raw_id, provider, fixture_id, bookmaker_id, provider_market_label,"
            " value_label, market_key, selection, line_q, price, source_update_ms, captured_at_ms,"
            " kickoff_in_payload_ms) VALUES (?,?,?,1001,?,?,?,?,?,?,?,?,?,?)",
            (run, raw, provider, book, label, value, market, selection, line_q, price, captured - MIN,
             captured, kickoff)).lastrowid

    def pair(self, over=1.90, under=1.92):
        self.quote(price=over)
        self.quote(selection="under", value="Under 2.5", price=under)

    # --- OddsPapi history fixtures ------------------------------------------------------
    def op_world(self, start_ms=KO_PAST, home_key="17", away_key="44"):
        """A finished fixture 2001 with an OddsPapi candidate, mapping and a history parse run."""
        self.add_fixture(2001, KO_PAST)
        self.op_slot = self.slot("op_history", NOW - 5 * MIN)
        req = self.request(self.op_slot, "/v4/fixtures", "tournamentId=17", provider="oddspapi")
        self.op_raw_fx = self.raw(req, 1, None, NOW - 5 * MIN, paged=0)
        self.con.execute("INSERT INTO provider_fixture_candidate VALUES ('oddspapi','idX',?, '17', ?, ?,"
                         " 'Home FC', 'Away FC', ?, '2')", (self.op_raw_fx, home_key, away_key, start_ms))
        req_h = self.request(self.op_slot, "/v4/historical-odds", "fixtureId=idX", provider="oddspapi")
        self.op_raw_h = self.raw(req_h, 1, None, NOW - 4 * MIN, paged=0)
        self.con.execute("INSERT INTO parse_run (parse_run_id, provider, kind, parser_git_commit, started_at_ms)"
                         " VALUES (2, 'oddspapi', 'history', ?, ?)", (COMMIT, NOW - 3 * MIN))

    def op_map(self, fid=2001, pid="idX", home="17", away="44", start=KO_PAST, kickoff=KO_PAST):
        self.con.execute("INSERT INTO provider_fixture_map (provider, provider_fixture_id, fixture_id, home_team_key,"
                         " away_team_key, provider_start_ms, kickoff_at_map_ms, evidence, mapped_at_ms)"
                         " VALUES ('oddspapi', ?, ?, ?, ?, ?, ?, 'teams+kickoff exact', ?)",
                         (pid, fid, home, away, start, kickoff, NOW))

    def hist(self, value="1010", selection="over", price=1.95, valid_from=KO_PAST - 30 * MIN, active=1,
             fid=2001, pid="idX"):
        return self.con.execute(
            "INSERT INTO price_history (parse_run_id, raw_id, provider, provider_fixture_id, fixture_id, bookmaker_id,"
            " provider_market_label, value_label, market_key, selection, line_q, valid_from_ms, price, active,"
            " max_stake) VALUES (2, ?, 'oddspapi', ?, ?, 4, ?, ?, ?, ?, 10, ?, ?, ?, 1000.0)",
            (self.op_raw_h, pid, fid, OP_GOALS, value, GOALS_FT, selection, valid_from, price, active)).lastrowid


@pytest.fixture
def w():
    return World()


# --- connection contract --------------------------------------------------------------

def test_raw_sqlite_fk_trap_is_real():
    con = sqlite3.connect(":memory:")
    con.executescript("CREATE TABLE p (id INTEGER PRIMARY KEY) STRICT;"
                      "CREATE TABLE c (pid INTEGER REFERENCES p(id)) STRICT;")
    con.execute("INSERT INTO c VALUES (999)")  # passes silently: FKs off by default


def test_connect_enforces_fks(w):
    fires(w.con, "INSERT INTO fixture_state_obs (fixture_id, raw_id, observed_at_ms, kickoff_ms, status_short)"
                 " VALUES (99999, 1, 0, 0, 'NS')", match="FOREIGN KEY")


def test_contract_fails_on_empty_db():
    with pytest.raises(ConnectionContractError, match="no tables"):
        assert_schema_contract(sqlite3.connect(":memory:"))


def test_contract_fails_on_loose_table():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (x INTEGER)")
    with pytest.raises(ConnectionContractError, match="non-STRICT"):
        assert_schema_contract(con)


def test_contract_fails_on_wrong_schema_version():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (x INTEGER) STRICT")
    with pytest.raises(ConnectionContractError, match="user_version"):
        assert_schema_contract(con)


def test_create_refuses_existing_db(tmp_path):
    p = tmp_path / "x.db"
    connect(p, create=True).close()
    with pytest.raises(ConnectionContractError, match="already has tables"):
        connect(p, create=True)
    connect(p).close()   # reopening without create is fine


def test_strict_rejects_wrong_type(w):
    fires(w.con, "INSERT INTO team VALUES ('not-an-int', 'Z')")


def test_no_venue_capture_tables_remain(w):
    names = {r[0] for r in w.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {n for n in names if n.startswith("sb_")}
    fires(w.con, "INSERT INTO bookmaker VALUES (99,'SportyBet','venue')", (), "CHECK")


# --- RAW layer accepts degraded responses ---------------------------------------------

def flags(w, raw_id):
    return w.con.execute("SELECT clock_ok, usable FROM raw_response WHERE raw_id=?", (raw_id,)).fetchone()


def test_raw_accepts_clock_skew_and_flags_it(w):
    req = w.request(w.slot_od, "/odds", "skew")
    assert flags(w, w.raw(req, 1, 1, REFRESH, skew=30_000)) == (0, 0)


def test_raw_accepts_slow_response_and_it_is_usable(w):
    req = w.request(w.slot_od, "/odds", "slow")
    assert flags(w, w.raw(req, 1, 1, REFRESH, latency=200_000, date=REFRESH - 100_000)) == (1, 1)


def test_raw_accepts_missing_date_header(w):
    req = w.request(w.slot_od, "/odds", "nodate")
    assert flags(w, w.raw(req, 1, 1, REFRESH, date=None)) == (0, 0)


def test_raw_accepts_error_response(w):
    req = w.request(w.slot_od, "/odds", "err")
    assert flags(w, w.raw(req, 1, None, REFRESH, status=500, errors=None)) == (1, 0)


def test_unpaged_provider_usable_without_paging(w):
    req = w.request(w.slot_od, "/v4/account", "", provider="oddspapi")
    assert flags(w, w.raw(req, 1, None, REFRESH, paged=0)) == (1, 1)


def test_paged_flag_must_match_provider(w):
    req = w.request(w.slot_od, "/v4/account", "x", provider="oddspapi")
    with pytest.raises(sqlite3.IntegrityError, match="paged flag"):
        w.raw(req, 1, 1, REFRESH, paged=1)


def test_raw_retry_is_a_new_attempt(w):
    req = w.request(w.slot_od, "/odds", "retry")
    w.raw(req, 1, 1, REFRESH, status=429, errors='["rate"]')
    w.raw(req, 1, 1, REFRESH + MIN, attempt=2)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        w.raw(req, 1, 1, REFRESH + 2 * MIN, attempt=2)


def test_endpoint_not_allowlisted_fires(w):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        w.request(w.slot_od, "/odds/live", "x")


# --- request completeness and slot ledger --------------------------------------------------

def complete_req(w, req):
    w.con.execute("UPDATE api_request SET status='complete' WHERE request_id=?", (req,))


def test_request_complete_missing_page_fires(w):
    req = w.request(w.slot_od, "/odds", "league=39", expected_pages=2)
    w.raw(req, 1, 2, REFRESH)
    with pytest.raises(sqlite3.IntegrityError, match="no usable attempt"):
        complete_req(w, req)


def test_request_complete_only_unusable_attempt_fires(w):
    req = w.request(w.slot_od, "/odds", "league=39", expected_pages=1)
    w.raw(req, 1, 1, REFRESH, skew=60_000)
    with pytest.raises(sqlite3.IntegrityError, match="no usable attempt"):
        complete_req(w, req)


def test_request_complete_after_good_retry(w):
    req = w.request(w.slot_od, "/odds", "league=39", expected_pages=1)
    w.raw(req, 1, 1, REFRESH, status=500, errors='["boom"]')
    w.raw(req, 1, 1, REFRESH + MIN, attempt=2)
    complete_req(w, req)


def test_request_complete_total_mismatch_fires(w):
    req = w.request(w.slot_od, "/odds", "league=39", expected_pages=1)
    w.raw(req, 1, 3, REFRESH)
    with pytest.raises(sqlite3.IntegrityError, match="paging_total"):
        complete_req(w, req)


def test_slot_complete_with_no_requests_fires(w):
    s = w.slot("af_odds", REFRESH + 185 * MIN)
    w.con.execute("UPDATE capture_slot SET expected_requests=3, expected_basis='3 league-dates' WHERE slot_id=?", (s,))
    fires(w.con, "UPDATE capture_slot SET status='complete', completed_at_ms=? WHERE slot_id=?",
          (REFRESH + 186 * MIN, s), "expected_requests")


def test_slot_zero_work_needs_reason(w):
    s = w.slot("af_odds", REFRESH + 185 * MIN)
    fires(w.con, "UPDATE capture_slot SET expected_requests=0, expected_basis='fixture table' WHERE slot_id=?", (s,), "CHECK")
    w.con.execute("UPDATE capture_slot SET expected_requests=0, expected_basis='fixture table',"
                  " note='no fixtures in window' WHERE slot_id=?", (s,))


def test_slot_complete_good(w):
    w.con.execute("UPDATE api_request SET expected_pages=1, status='complete' WHERE request_id=?", (w.req_od,))
    w.con.execute("UPDATE capture_slot SET expected_requests=1, expected_basis='1 fixture' WHERE slot_id=?", (w.slot_od,))
    w.con.execute("UPDATE capture_slot SET status='complete', completed_at_ms=? WHERE slot_id=?",
                  (REFRESH + 7 * MIN, w.slot_od))


# --- snapshot quotes -----------------------------------------------------------------------

def test_quote_from_unusable_raw_fires(w):
    req = w.request(w.slot_od, "/odds", "bad")
    raw = w.raw(req, 1, 1, REFRESH, skew=60_000)
    with pytest.raises(sqlite3.IntegrityError, match="not usable"):
        w.quote(raw=raw)


def test_quote_from_unmapped_book_fires(w):
    w.con.execute("INSERT INTO bookmaker VALUES (7,'SoftBook','observed')")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        w.quote(book=7)


def test_quote_unlisted_label_fires(w):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        w.quote(value="Over 2.5 Both Halves")


def test_quote_first_half_market_mapped_to_full_match_fires(w):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        w.quote(label="6|Goals Over/Under First Half", market=GOALS_FT)


def test_quote_duplicate_key_in_same_payload_fires(w):
    w.quote()
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        w.quote(price=2.05)


def test_quote_post_kickoff_payload_fires(w):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        w.quote(kickoff=REFRESH)


def test_quote_post_kickoff_per_fixture_table_fires(w):
    w.add_fixture(1002, REFRESH - MIN)   # already kicked off at capture time
    req = w.request(w.slot_od, "/odds", "late")
    raw = w.raw(req, 1, 1, REFRESH)
    with pytest.raises(sqlite3.IntegrityError, match="already passed"):
        w.con.execute(
            "INSERT INTO quote (parse_run_id, raw_id, provider, fixture_id, bookmaker_id, provider_market_label,"
            " value_label, market_key, selection, line_q, price, captured_at_ms, kickoff_in_payload_ms)"
            " VALUES (1,?, 'api_football', 1002, 4, ?, 'Over 2.5', ?, 'over', 10, 1.9, ?, ?)",
            (raw, AF_GOALS, GOALS_FT, REFRESH, KO))


def test_quote_into_history_run_fires(w):
    w.op_world()
    with pytest.raises(sqlite3.IntegrityError, match="snapshot run"):
        w.quote(run=2)


# --- quarantine ------------------------------------------------------------------------------

def complete_run(con, n, run=1):
    con.execute("UPDATE parse_run SET status='complete', declared_row_count=? WHERE parse_run_id=?", (n, run))


def quarantine(w, reason, label=AF_GOALS):
    w.con.execute("INSERT INTO quarantine VALUES (1, ?, 4, ?, ?, 'test')", (w.raw_od, label, reason))


def test_incomplete_group_unquarantined_fires(w):
    w.quote()
    with pytest.raises(sqlite3.IntegrityError, match="bad group not quarantined"):
        complete_run(w.con, 1)


def test_incomplete_group_quarantined_completes(w):
    w.quote()
    for v, s, p in (("Home", "home", 2.0), ("Draw", "draw", 3.4), ("Away", "away", 3.9)):
        w.quote(label="1|Match Winner", market="MATCH_RESULT_FT", value=v, selection=s, line_q=0, price=p)
    quarantine(w, "incomplete_group")
    complete_run(w.con, 4)
    w.con.execute("UPDATE parse_run SET is_current=1 WHERE parse_run_id=1")
    clean = w.con.execute("SELECT market_key, count(*) FROM v_quote_clean GROUP BY market_key").fetchall()
    assert clean == [("MATCH_RESULT_FT", 3)]


def test_three_way_missing_draw_detected(w):
    w.quote(label="1|Match Winner", market="MATCH_RESULT_FT", value="Home", selection="home", line_q=0, price=2.0)
    w.quote(label="1|Match Winner", market="MATCH_RESULT_FT", value="Away", selection="away", line_q=0, price=3.9)
    with pytest.raises(sqlite3.IntegrityError, match="bad group not quarantined"):
        complete_run(w.con, 2)


def test_sub_one_overround_unquarantined_fires(w):
    w.pair(over=2.40, under=2.10)
    with pytest.raises(sqlite3.IntegrityError, match="bad group not quarantined"):
        complete_run(w.con, 2)


def test_unknown_label_unquarantined_fires(w):
    w.pair()
    w.con.execute("INSERT INTO unmapped_label VALUES (1, ?, 4, ?, 'Over 2.5 Both Halves', 1)", (w.raw_od, AF_GOALS))
    with pytest.raises(sqlite3.IntegrityError, match="unknown label"):
        complete_run(w.con, 2)


def test_unknown_label_quarantined_completes(w):
    w.pair()
    w.con.execute("INSERT INTO unmapped_label VALUES (1, ?, 4, ?, 'Over 2.5 Both Halves', 1)", (w.raw_od, AF_GOALS))
    quarantine(w, "unknown_label")
    complete_run(w.con, 2)


def test_parse_run_count_mismatch_fires(w):
    w.pair()
    with pytest.raises(sqlite3.IntegrityError, match="declared_row_count"):
        complete_run(w.con, 3)


def test_parse_run_empty_needs_reason(w):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        complete_run(w.con, 0)


# --- cross-provider fixture mapping (requirement 3) ---------------------------------------

def test_mapping_good(w):
    w.op_world()
    w.op_map()


def test_mapping_team_not_allowlisted_fires(w):
    w.op_world(home_key="999")
    with pytest.raises(sqlite3.IntegrityError, match="home team not in exact allowlist"):
        w.op_map(home="999")


def test_mapping_swapped_orientation_fires(w):
    w.op_world(home_key="44", away_key="17")
    with pytest.raises(sqlite3.IntegrityError, match="home team"):
        w.op_map(home="44", away="17")


def test_mapping_wrong_fixture_same_teams_different_day_fires(w):
    w.op_world()
    w.add_fixture(2002, KO_PAST + 7 * 24 * 60 * MIN)   # reverse-fixture-like: same teams a week later
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        w.op_map(fid=2002, kickoff=KO_PAST + 7 * 24 * 60 * MIN)


def test_mapping_kickoff_not_latest_fires(w):
    w.op_world()
    with pytest.raises(sqlite3.IntegrityError, match="latest known kickoff"):
        w.op_map(kickoff=KO_PAST + 5 * MIN)


def test_mapping_without_candidate_fires(w):           # ABSENT: provider never listed it
    w.op_world()
    with pytest.raises(sqlite3.IntegrityError, match="no provider candidate"):
        w.op_map(pid="idNEVERLISTED")


def test_mapping_one_to_one_provider_side(w):
    w.op_world()
    w.op_map()
    w.add_fixture(2003, KO_PAST)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE|PRIMARY"):
        w.op_map(fid=2003)


def test_mapping_one_to_one_fixture_side(w):
    w.op_world()
    w.op_map()
    w.con.execute("INSERT INTO provider_fixture_candidate VALUES ('oddspapi','idY',?, '17', '17', '44',"
                  " 'Home FC', 'Away FC', ?, '2')", (w.op_raw_fx, KO_PAST))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        w.op_map(pid="idY")


def test_mapping_revocation_frees_fixture_for_reissued_id(w):
    w.op_world()
    w.op_map()
    w.con.execute("INSERT INTO provider_fixture_candidate VALUES ('oddspapi','idY',?, '17', '17', '44',"
                  " 'Home FC', 'Away FC', ?, '2')", (w.op_raw_fx, KO_PAST))
    w.con.execute("UPDATE provider_fixture_map SET revoked=1, revoked_reason='reissued as idY'"
                  " WHERE provider_fixture_id='idX'")
    w.op_map(pid="idY")


def test_mapping_edit_fires(w):
    w.op_world()
    w.op_map()
    fires(w.con, "UPDATE provider_fixture_map SET fixture_id = 1001", (), "append-only")


def test_misjoin_audit_and_rate(w):
    w.op_world()
    w.op_map()
    w.con.execute("INSERT INTO mapping_audit (provider, provider_fixture_id, method, agrees, detail, evidence_raw_id,"
                  " recorded_at_ms) VALUES ('oddspapi','idX','final_score',0,'1-0 vs 2-2',?,?)", (w.op_raw_fx, NOW))
    assert w.con.execute("SELECT checked, disagreements, rate FROM v_misjoin_rate").fetchone() == (1, 1, 1.0)


def test_misjoin_audit_needs_evidence(w):
    w.op_world()
    w.op_map()
    fires(w.con, "INSERT INTO mapping_audit (provider, provider_fixture_id, method, agrees, detail, recorded_at_ms)"
                 " VALUES ('oddspapi','idX','manual',1,'looked fine',?)", (NOW,), "CHECK")


# --- price history and the closing price -------------------------------------------------------

def test_history_requires_live_mapping(w):              # ABSENT mapping
    w.op_world()
    with pytest.raises(sqlite3.IntegrityError):
        w.hist()


def test_history_on_revoked_mapping_fires(w):
    w.op_world()
    w.op_map()
    w.con.execute("UPDATE provider_fixture_map SET revoked=1, revoked_reason='mis-join' WHERE provider_fixture_id='idX'")
    with pytest.raises(sqlite3.IntegrityError, match="live provider mapping"):
        w.hist()


def test_history_wrong_fixture_fires(w):
    w.op_world()
    w.op_map()
    with pytest.raises(sqlite3.IntegrityError, match="live provider mapping"):
        w.hist(fid=1001)


def test_history_into_snapshot_run_fires(w):
    w.op_world()
    w.op_map()
    with pytest.raises(sqlite3.IntegrityError, match="history run"):
        w.con.execute(
            "INSERT INTO price_history (parse_run_id, raw_id, provider, provider_fixture_id, fixture_id, bookmaker_id,"
            " provider_market_label, value_label, market_key, selection, line_q, valid_from_ms, price, active)"
            " VALUES (1, ?, 'oddspapi', 'idX', 2001, 4, ?, '1010', ?, 'over', 10, ?, 1.9, 1)",
            (w.op_raw_h, OP_GOALS, GOALS_FT, KO_PAST - MIN))


def test_history_record_from_the_future_fires(w):
    w.op_world()
    w.op_map()
    with pytest.raises(sqlite3.IntegrityError, match="after the response"):
        w.hist(valid_from=NOW + 60 * MIN)


def test_close_is_last_active_pre_kickoff_record(w):
    w.op_world()
    w.op_map()
    w.hist(price=1.80, valid_from=KO_PAST - 300 * MIN)
    w.hist(price=1.95, valid_from=KO_PAST - 20 * MIN)
    w.hist(price=2.10, valid_from=KO_PAST - 5 * MIN, active=0)       # suspended: not the close
    w.hist(price=2.50, valid_from=KO_PAST + 10 * MIN)                 # in-play: not the close
    w.hist(value="1011", selection="under", price=1.92, valid_from=KO_PAST - 2 * MIN)
    complete_run(w.con, 5, run=2)
    w.con.execute("UPDATE parse_run SET is_current=1 WHERE parse_run_id=2")
    rows = dict((r[0], r[1:]) for r in w.con.execute(
        "SELECT selection, close_price, market_age_at_ko_ms, book_silence_at_ko_ms FROM v_close"))
    assert rows["over"] == (1.95, 20 * MIN, 2 * MIN)
    assert rows["under"] == (1.92, 2 * MIN, 2 * MIN)


def test_close_absent_when_no_pre_kickoff_record(w):    # ABSENT close must not be invented
    w.op_world()
    w.op_map()
    w.hist(price=2.50, valid_from=KO_PAST + 10 * MIN)
    complete_run(w.con, 1, run=2)
    w.con.execute("UPDATE parse_run SET is_current=1 WHERE parse_run_id=2")
    assert w.con.execute("SELECT count(*) FROM v_close").fetchone()[0] == 0


# --- coverage ledger ---------------------------------------------------------------------------

def coverage_setup(w):
    w.con.execute("INSERT INTO coverage_expectation VALUES (1, 'oddspapi', 4, 'goals_total', 0.8)")


def cov(w, denom, obs, share=0.8, reason=None, day=0):
    w.con.execute("INSERT INTO coverage_check (day_start_ms, prereg_id, provider, bookmaker_id, market_family,"
                  " denominator, observed, min_share, no_fixtures_reason) VALUES (?,1,'oddspapi',4,"
                  "'goals_total',?,?,?,?)", (day, denom, obs, share, reason))
    return w.con.execute("SELECT passed FROM coverage_check WHERE day_start_ms=?", (day,)).fetchone()[0]


def test_coverage_book_dropped_to_zero_fails(w):
    coverage_setup(w)
    assert cov(w, 120, 0) == 0


def test_coverage_zero_denominator_without_reason_fails(w):
    coverage_setup(w)
    assert cov(w, 0, 0) == 0


def test_coverage_zero_denominator_with_reason_passes(w):
    coverage_setup(w)
    assert cov(w, 0, 0, reason="international break") == 1


def test_coverage_threshold_tampering_fires(w):
    coverage_setup(w)
    with pytest.raises(sqlite3.IntegrityError, match="pre-registered"):
        cov(w, 120, 50, share=0.4)


# --- forward evaluation, strategies, picks ---------------------------------------------------

def setup_model(w):
    w.con.execute("INSERT INTO model_version VALUES ('m0', ?, 1)", (COMMIT,))
    w.con.execute("INSERT INTO strategy VALUES ('s0', 1, 'rule', 1, 'test rule')")


def eval_sql():
    return ("INSERT INTO evaluation (model_version, fixture_id, market_key, selection, line_q, p_model,"
            " evaluated_at_ms, feature_cutoff_ms, kickoff_known_ms, ref_quote_id_at_eval) VALUES ('m0',1001,"
            "?,'over',10,0.55,?,?,?,?)")


def now_ms():
    return int(time.time() * 1000)


def test_eval_good(w):
    setup_model(w)
    q = w.quote()
    t = now_ms()
    w.con.execute(eval_sql(), (GOALS_FT, t, REFRESH, KO, q))


def test_eval_backfilled_forecast_fires(w):
    setup_model(w)
    fires(w.con, eval_sql(), (GOALS_FT, REFRESH, REFRESH - MIN, KO, None), "database clock")


def test_eval_feature_after_forecast_fires(w):
    setup_model(w)
    t = now_ms()
    fires(w.con, eval_sql(), (GOALS_FT, t, t + MIN, KO, None), "CHECK")


def test_eval_reference_wrong_selection_fires(w):
    setup_model(w)
    q = w.quote(selection="under", value="Under 2.5")
    fires(w.con, eval_sql(), (GOALS_FT, now_ms(), REFRESH, KO, q), "another fixture")


def test_eval_stale_kickoff_after_reschedule_fires(w):
    setup_model(w)
    w.con.execute("INSERT INTO fixture_state_obs (fixture_id, raw_id, observed_at_ms, kickoff_ms, status_short)"
                  " VALUES (1001, ?, ?, ?, 'NS')", (w.raw_od, REFRESH, KO + 24 * 60 * MIN))
    fires(w.con, eval_sql(), (GOALS_FT, now_ms(), REFRESH, KO, None), "latest kickoff")


def test_eval_after_outcome_recorded_fires(w):
    setup_model(w)
    w.con.execute("INSERT INTO outcome_obs (fixture_id, source, raw_id, recorded_at_ms, status_short, goals_home_90,"
                  " goals_away_90) VALUES (1001, 'api_football', ?, ?, 'FT', 1, 0)", (w.raw_fx, REFRESH))
    fires(w.con, eval_sql(), (GOALS_FT, now_ms(), REFRESH, KO, None), "outcome")


def test_strategy_slot_cap(w):
    fires(w.con, "INSERT INTO strategy VALUES ('s7', 1, 'rule', 7, 'seventh rule')", (), "CHECK")


def test_strategy_slot_reuse_fires(w):
    setup_model(w)
    fires(w.con, "INSERT INTO strategy VALUES ('s1', 1, 'rule', 1, 'same slot')", (), "UNIQUE")


def test_rule_without_slot_fires(w):
    fires(w.con, "INSERT INTO strategy VALUES ('s9', 1, 'rule', NULL, 'unbudgeted rule')", (), "CHECK")


def test_strategy_frozen(w):
    setup_model(w)
    fires(w.con, "UPDATE strategy SET description='edited after seeing data'", (), "frozen")


def make_eval(w):
    setup_model(w)
    q = w.quote()
    return w.con.execute(eval_sql(), (GOALS_FT, now_ms(), REFRESH, KO, q)).lastrowid


def test_pick_good_and_unique(w):
    e = make_eval(w)
    w.con.execute("INSERT INTO pick VALUES ('s0', 1001, ?, ?)", (e, now_ms()))
    fires(w.con, "INSERT INTO pick VALUES ('s0', 1001, ?, ?)", (e, now_ms()), "UNIQUE")


def test_pick_backdated_fires(w):
    e = make_eval(w)
    fires(w.con, "INSERT INTO pick VALUES ('s0', 1001, ?, ?)", (e, REFRESH), "database clock")


def test_pick_before_prereg_effective_fires(w):
    e = make_eval(w)
    w.con.execute("INSERT INTO preregistration VALUES (2,'p2',?, 'v2', ?, NULL, ?, ?)",
                  (COMMIT, "d" * 64, NOW + 60 * MIN, NOW + 60 * MIN))
    w.con.execute("INSERT INTO strategy VALUES ('s1', 2, 'rule', 2, 'chosen from exploration data')")
    fires(w.con, "INSERT INTO pick VALUES ('s1', 1001, ?, ?)", (e, now_ms()), "pre-registration")


def test_pick_is_frozen(w):
    e = make_eval(w)
    w.con.execute("INSERT INTO pick VALUES ('s0', 1001, ?, ?)", (e, now_ms()))
    fires(w.con, "UPDATE pick SET frozen_at_ms = frozen_at_ms + 1", (), "frozen")
    fires(w.con, "UPDATE evaluation SET p_model = 0.6", (), "frozen")


# --- bet slips (execution data only for bets placed) ---------------------------------------------

def slip_sql():
    return ("INSERT INTO bet_slip (ticket_id, placed_at_ms, fixture_id, market_key, selection, line_q, odds_taken,"
            " stake_minor, legs, strategy_id, min_acceptable_price, slip_screenshot_sha256, recorded_at_ms)"
            " VALUES (?, ?, 1001, ?, 'over', 10, 1.95, 100000, ?, ?, 1.90, ?, ?)")


def test_slip_good_unlinked(w):
    w.con.execute(slip_sql(), ("T1", NOW, GOALS_FT, 1, None, SHA, NOW))


def test_slip_accumulator_fires(w):
    fires(w.con, slip_sql(), ("T1", NOW, GOALS_FT, 3, None, SHA, NOW), "CHECK")


def test_slip_after_kickoff_fires(w):
    fires(w.con, slip_sql(), ("T1", KO + MIN, GOALS_FT, 1, None, SHA, KO + MIN), "kickoff")


def test_slip_linked_to_unknown_pick_fires(w):
    setup_model(w)
    fires(w.con, slip_sql(), ("T1", NOW, GOALS_FT, 1, "s0", SHA, NOW), "FOREIGN KEY")


def test_slip_before_pick_frozen_fires(w):
    e = make_eval(w)
    w.con.execute("INSERT INTO pick VALUES ('s0', 1001, ?, ?)", (e, now_ms()))
    fires(w.con, slip_sql(), ("T1", REFRESH, GOALS_FT, 1, "s0", SHA, now_ms()), "before the linked pick")


# --- outcomes and labels -----------------------------------------------------------------------------

def outcome(w, fid, status, corners=(6, 5), goals=(1, 1)):
    return w.con.execute(
        "INSERT INTO outcome_obs (fixture_id, source, raw_id, recorded_at_ms, status_short, goals_home_90,"
        " goals_away_90, corners_home, corners_away, corners_raw_id) VALUES (?, 'api_football', ?, ?, ?, ?, ?, ?, ?, ?)",
        (fid, w.raw_fx, KO + 150 * MIN, status, goals[0], goals[1], corners[0], corners[1],
         w.raw_fx if corners[0] is not None else None)).lastrowid


def test_corners_without_provenance_fires(w):
    fires(w.con, "INSERT INTO outcome_obs (fixture_id, source, raw_id, recorded_at_ms, status_short, goals_home_90,"
                 " goals_away_90, corners_home, corners_away) VALUES (1001, 'api_football', ?, ?, 'FT', 1, 1, 5, 4)",
          (w.raw_fx, KO + 150 * MIN), "CHECK")


def label(w, fid, family, status, reason, oid):
    w.con.execute("INSERT INTO eval_label VALUES (?, ?, 'r1', ?, ?, ?, ?)",
                  (fid, family, status, reason, oid, KO + 160 * MIN))


def test_ft_without_goals_fires(w):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        outcome(w, 1001, "FT", goals=(None, None))


def test_corners_on_et_possible_fixture_must_be_excluded(w):
    w.add_fixture(1003, KO, et_possible=1)
    oid = outcome(w, 1003, "FT")
    with pytest.raises(sqlite3.IntegrityError, match="excluded ex ante"):
        label(w, 1003, "corners", "included", None, oid)
    label(w, 1003, "corners", "excluded", "ex_ante_et_possible", None)


def test_goals_label_on_aet_fixture_uses_90_minute_score(w):
    w.add_fixture(1003, KO, et_possible=1)
    oid = outcome(w, 1003, "AET")
    label(w, 1003, "goals_total", "included", None, oid)


def test_no_usable_close_exclusion(w):
    label(w, 1001, "goals_total", "excluded", "no_usable_close", None)


# --- append-only ---------------------------------------------------------------------------------------

def test_raw_response_append_only(w):
    fires(w.con, "UPDATE raw_response SET http_status=500", (), "append-only")
    fires(w.con, "DELETE FROM raw_response", (), "append-only")


def test_prereg_only_ots_fill_allowed(w):
    w.con.execute("UPDATE preregistration SET ots_proof_path='prereg/p0.ots' WHERE prereg_id=1")
    fires(w.con, "UPDATE preregistration SET effective_from_ms = 0 WHERE prereg_id=1", (), "append-only")
