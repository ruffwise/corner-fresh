"""Tests for the closing-price rule close-v1 (collector/close.py).

The property that matters most: NO record at or after the cutoff can ever
become, or change, a close. It is tested on synthetic series, by mutation
(appending extreme post-cutoff records must change nothing), and on every
real OddsPapi history payload committed under tests/fixtures/real/.
"""
from __future__ import annotations

import gzip
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from collector import close  # noqa: E402
from collector.close import Record  # noqa: E402

MIN = 60_000
KO = close.iso_ms("2026-10-10T11:30:00Z")
REAL = ROOT / "tests" / "fixtures" / "real" / "oddspapi"


# --- cutoff -----------------------------------------------------------------------------------

def test_cutoff_is_min_of_sources_minus_buffer():
    c = close.cutoff(KO, "FT", af_actual_start_ms=KO + 3 * MIN, op_true_start_ms=KO + 2 * MIN)
    assert c.cutoff_ms == KO - 2 * MIN and c.excluded_reason is None


def test_cutoff_uses_earlier_actual_start():
    c = close.cutoff(KO, "FT", op_true_start_ms=KO - 4 * MIN)     # started early
    assert c.cutoff_ms == KO - 6 * MIN


def test_cutoff_sources_disagree_means_no_close():
    c = close.cutoff(KO, "FT", af_actual_start_ms=KO + 1 * MIN, op_true_start_ms=KO + 25 * MIN)
    assert c.cutoff_ms is None and c.excluded_reason == "start_sources_disagree"


def test_cutoff_scheduled_vs_actual_disagree_means_no_close():
    c = close.cutoff(KO, "FT", af_actual_start_ms=KO + 45 * MIN)  # delayed kick-off beyond tolerance
    assert c.excluded_reason == "start_sources_disagree"


@pytest.mark.parametrize("status", ["NS", "PST", "ABD", "1H", "CANC"])
def test_cutoff_requires_finished_match(status):
    assert close.cutoff(KO, status).excluded_reason == "status_not_finished"


def test_cutoff_with_scheduled_only_is_labelled():
    c = close.cutoff(KO, "FT")
    assert c.cutoff_ms == KO - 2 * MIN and c.basis.startswith("min(af_scheduled)")


# --- series close -----------------------------------------------------------------------------

CUT = KO - 2 * MIN


def test_close_is_record_in_force_at_cutoff():
    s = close.close_series([Record(KO - 60 * MIN, 2.0, True), Record(KO - 20 * MIN, 2.1, True),
                            Record(KO - 1 * MIN, 2.3, True), Record(KO + 5 * MIN, 9.0, True)], CUT)
    assert (s.price, s.record_ms, s.n_in_buffer, s.n_after_cutoff) == (2.1, KO - 20 * MIN, 1, 2)


def test_suspended_in_force_means_no_close_and_no_fallback():
    s = close.close_series([Record(KO - 30 * MIN, 2.0, True), Record(KO - 5 * MIN, 2.0, False)], CUT)
    assert s.price is None and s.reason == "suspended_at_cutoff"


def test_no_pre_cutoff_record_means_no_close():
    s = close.close_series([Record(KO + 1 * MIN, 2.0, True)], CUT)
    assert s.price is None and s.reason == "no_record_before_cutoff"


def test_last_change_differs_from_last_record_when_prices_repeat():
    s = close.close_series([Record(KO - 90 * MIN, 1.9, True), Record(KO - 70 * MIN, 2.0, True),
                            Record(KO - 40 * MIN, 2.0, True), Record(KO - 3 * MIN, 2.0, True)], CUT)
    assert s.record_ms == KO - 3 * MIN and s.last_change_ms == KO - 70 * MIN


def test_ambiguous_tie_means_no_close():
    s = close.close_series([Record(KO - 10 * MIN, 2.0, True), Record(KO - 10 * MIN, 2.2, True)], CUT)
    assert s.price is None and s.reason == "ambiguous_tie_at_last_record"


def test_record_exactly_at_cutoff_is_excluded():
    s = close.close_series([Record(KO - 10 * MIN, 2.0, True), Record(CUT, 3.0, True)], CUT)
    assert s.price == 2.0 and s.n_in_buffer == 1


def test_mutation_post_cutoff_records_never_change_a_close():
    rnd = random.Random(7)
    for _ in range(500):
        base = [Record(KO - rnd.randint(3, 600) * MIN // 1 + rnd.randint(0, 59_999), round(rnd.uniform(1.1, 9), 2),
                       rnd.random() > 0.1) for _ in range(rnd.randint(1, 30))]
        before = close.close_series(base, CUT)
        extra = [Record(CUT + rnd.randint(0, 200 * MIN), rnd.choice([1.01, 50.0, 1000.0]), rnd.random() > 0.5)
                 for _ in range(rnd.randint(1, 50))]
        after = close.close_series(base + extra, CUT)
        assert (before.price, before.record_ms, before.last_change_ms, before.reason) == \
               (after.price, after.record_ms, after.last_change_ms, after.reason)


# --- payload-level report -------------------------------------------------------------------------

def payload(series: dict) -> dict:
    markets = {}
    for (m, o), recs in series.items():
        markets.setdefault(m, {"outcomes": {}})["outcomes"][o] = {"players": {"0": [
            {"createdAt": t, "price": p, "limit": 1, "active": a} for t, p, a in recs]}}
    return {"fixtureId": "idT", "bookmakers": {"pinnacle": {"markets": markets}}}


def test_report_counts_and_inplay_exclusion():
    body = payload({("101", "101"): [("2026-10-10T11:00:00Z", 2.0, True), ("2026-10-10T11:27:00Z", 2.1, True),
                                     ("2026-10-10T11:29:30Z", 2.2, True), ("2026-10-10T11:45:00Z", 7.0, True)],
                    ("101", "102"): [("2026-10-10T11:00:00Z", 1.9, True), ("2026-10-10T11:26:00Z", 1.9, False)]})
    rep = close.fixture_close_report(body, close.cutoff(KO, "FT"))
    assert rep["series_total"] == 2 and rep["series_with_close"] == 1
    assert rep["series_without_close"] == {"suspended_at_cutoff": 1}
    assert rep["records_in_buffer"] == 1 and rep["records_at_or_after_cutoff"] == 2
    assert rep["book_silence_at_cutoff_min"] == 1.0


def test_report_on_excluded_fixture():
    rep = close.fixture_close_report(payload({}), close.cutoff(KO, "PST"))
    assert rep["excluded"] == "status_not_finished"


def test_payload_true_start_is_read():
    assert close.payload_true_start({"trueStartTime": "2026-10-10T11:31:00Z"}) == KO + MIN
    assert close.payload_true_start({}) is None


# --- look-ahead control ----------------------------------------------------------------------------

def test_control_flags_inplay_leak():
    rnd = random.Random(3)
    rows = []
    for i in range(400):
        y = rnd.random() < 0.5
        m20 = m10 = 0.5
        c = 0.5 + (0.25 if y else -0.25) * (rnd.random() < 0.1)   # 10% of closes saw a goal
        rows.append((i, m20, m10, c, int(y)))
    r = close.lookahead_control(rows, boot=2000)
    assert not r.passed and r.jumps_over_10pp


def test_control_passes_clean_market_with_informative_late_money():
    rnd = random.Random(4)
    rows = []
    for i in range(1500):
        y = rnd.random() < 0.5
        drift = 0.004 if y else -0.004            # the same informative drift in both windows
        m20 = 0.5
        m10 = m20 + drift + rnd.gauss(0, 0.01)
        c = m10 + drift + rnd.gauss(0, 0.01)
        rows.append((i, m20, m10, c, int(y)))
    r = close.lookahead_control(rows, boot=2000)
    assert r.passed
    assert r.ci_final_pp[0] > 0          # the literal "final move = 0" test would have failed here


def test_control_empty():
    assert close.lookahead_control([]).n == 0


# --- real payloads (exported from the archive by `collector export-fixtures`) --------------------------

def real_payloads():
    return sorted(REAL.glob("historical-odds_*.json.gz")) if REAL.exists() else []


@pytest.mark.parametrize("path", real_payloads(), ids=lambda p: p.name)
def test_real_history_no_post_cutoff_record_enters_close(path):
    doc = json.loads(gzip.decompress(path.read_bytes()))
    meta, body = doc["meta"], doc["body"]
    cut = close.cutoff(close.iso_ms(meta["scheduled_kickoff_utc"]), meta.get("status", "FT"),
                       op_true_start_ms=close.payload_true_start(body))
    assert cut.cutoff_ms is not None, cut
    series = close.series_from_history(body)
    assert series, "payload has no Pinnacle series"
    worst = 10 ** 18
    for key, recs in series.items():
        c = close.close_series(recs, cut.cutoff_ms)
        if c.record_ms is not None:
            assert c.record_ms < cut.cutoff_ms, key
        pre = [r for r in recs if r.t_ms < cut.cutoff_ms]
        c_pre = close.close_series(pre, cut.cutoff_ms)
        assert (c.price, c.record_ms, c.reason) == (c_pre.price, c_pre.record_ms, c_pre.reason), key
        poisoned = recs + [Record(cut.cutoff_ms + k * 1000, 999.0, True) for k in range(3)]
        assert close.close_series(poisoned, cut.cutoff_ms).price == c.price, key
        worst = min(worst, cut.cutoff_ms - (c.record_ms or cut.cutoff_ms - 1))
    assert worst > 0


# --- v0.5.2 pre-registered additions ----------------------------------------------------------------

def test_v1b_bridges_a_suspension_that_began_within_5_minutes():
    recs = [Record(CUT - 20 * MIN, 2.0, True), Record(CUT - 4 * MIN, 2.1, True), Record(CUT - 1 * MIN, 2.1, False)]
    v1 = close.close_series(recs, CUT)
    v1b = close.close_series(recs, CUT, bridge_ms=close.BRIDGE_MS)
    assert v1.price is None and v1.reason == "suspended_at_cutoff"          # v1 itself is unchanged
    assert v1b.price == 2.1 and v1b.bridged and v1b.record_ms == CUT - 4 * MIN


def test_v1b_does_not_bridge_an_older_suspension():
    recs = [Record(CUT - 20 * MIN, 2.0, True), Record(CUT - 6 * MIN, 2.0, False)]
    assert close.close_series(recs, CUT, bridge_ms=close.BRIDGE_MS).reason == "suspended_at_cutoff"


def test_v1b_never_uses_a_post_cutoff_record():
    recs = [Record(CUT - 3 * MIN, 2.0, True), Record(CUT - 1 * MIN, 2.0, False), Record(CUT + 1, 9.0, True)]
    c = close.close_series(recs, CUT, bridge_ms=close.BRIDGE_MS)
    assert c.price == 2.0 and c.record_ms < CUT


def test_v1b_equals_v1_when_in_force_record_is_active():
    rnd = random.Random(11)
    for _ in range(300):
        recs = [Record(CUT - rnd.randint(1, 300) * MIN, round(rnd.uniform(1.1, 9), 2), True)
                for _ in range(rnd.randint(1, 20))]
        a, b = close.close_series(recs, CUT), close.close_series(recs, CUT, bridge_ms=close.BRIDGE_MS)
        assert (a.price, a.record_ms, a.last_change_ms, a.reason) == (b.price, b.record_ms, b.last_change_ms, b.reason)


def test_g05a_needs_two_start_sources():
    assert not close.cutoff(KO, "FT").counts_toward_g05a
    assert close.cutoff(KO, "FT", af_actual_start_ms=KO + MIN).counts_toward_g05a
    assert not close.cutoff(KO, "FT", af_actual_start_ms=KO + 40 * MIN).counts_toward_g05a   # excluded fixture


CATALOG = [{"marketId": 101, "marketType": "1x2", "period": "fulltime", "handicap": 0.0, "marketName": "FT Result"},
           {"marketId": 1010, "marketType": "totals", "period": "fulltime", "handicap": 2.5},
           {"marketId": 1012, "marketType": "totals", "period": "fulltime", "handicap": 3.5},
           {"marketId": 1008, "marketType": "totals", "period": "fulltime", "handicap": 1.5}]


def iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_main_line_is_most_balanced_at_cutoff_minus_60_and_ignores_later_moves():
    t60, t5 = iso(CUT - 70 * MIN), iso(CUT - 5 * MIN)
    body = payload({("1010", "1010"): [(t60, 1.95, True), (t5, 1.50, True)],       # balanced at -60, moves late
                    ("1010", "1011"): [(t60, 1.95, True), (t5, 2.70, True)],
                    ("1012", "1012"): [(t60, 3.10, True), (t5, 1.98, True)],       # balanced only late
                    ("1012", "1013"): [(t60, 1.38, True), (t5, 1.92, True)],
                    ("1008", "1008"): [(t60, 1.30, True)], ("1008", "1009"): [(t60, 3.60, True)]})
    cat = close.market_catalog(CATALOG)
    assert close.main_lines(close.series_from_history(body), CUT, cat) == {"totals:fulltime": "1010"}


def test_report_by_market_and_family_shows_suspended_main_line():
    t60, t3, t1 = iso(CUT - 70 * MIN), iso(CUT - 3 * MIN), iso(CUT - 1 * MIN)
    body = payload({("101", "101"): [(t60, 2.0, True)], ("101", "102"): [(t60, 3.4, True)],
                    ("101", "103"): [(t60, 3.9, True)],
                    ("1010", "1010"): [(t60, 1.95, True), (t3, 1.95, True), (t1, 1.95, False)],   # main, suspended
                    ("1010", "1011"): [(t60, 1.95, True)],
                    ("1012", "1012"): [(t60, 3.1, True), (iso(CUT - 30 * MIN), 3.1, False)],       # alt line pulled
                    ("1012", "1013"): [(t60, 1.38, True), (iso(CUT - 30 * MIN), 1.38, False)]})
    rep = close.fixture_close_report(body, close.cutoff(KO, "FT", af_actual_start_ms=KO),
                                     catalog=close.market_catalog(CATALOG))
    assert rep["start_sources_present"] == 2 and rep["counts_toward_g05a"]
    assert rep["by_market"]["1012"]["suspended_at_cutoff"] == 2 and rep["by_market"]["1012"]["with_close_v1b"] == 0
    assert rep["by_market"]["1010"] == {"series": 2, "with_close_v1": 1, "suspended_at_cutoff": 1,
                                        "with_close_v1b": 2, "type": "totals", "period": "fulltime", "handicap": 2.5}
    tot = rep["by_family"]["totals:fulltime"]
    assert tot["main_market"] == "1010" and not tot["main_line_usable_v1"] and tot["main_line_usable_v1b"]
    assert rep["by_family"]["1x2:fulltime"]["main_line_usable_v1"]
    d = {(x["market"], x["outcome"]): x for x in rep["suspended_at_cutoff_detail"]}
    assert d[("1010", "1010")]["last_active_before_cutoff_min"] == 3.0 and d[("1010", "1010")]["bridged_by_v1b"]
    assert d[("1012", "1012")]["suspended_for_min"] == 30.0 and not d[("1012", "1012")]["bridged_by_v1b"]


def control(n=500, flags=()):
    rows = [(f"f{i}", 0.5, 0.5, 0.5, i % 2) for i in range(n)]
    for i in flags:
        rows[i] = (f"f{i}", 0.5, 0.5, 0.62, 0)       # a 12pp jump against the outcome: flags, does not fail L1
    return close.lookahead_control(rows, boot=500)


def test_l2_flags_go_to_review_not_failure():
    c = control(flags=(3,))
    assert c.passed and len(c.jumps_over_10pp) == 1                 # L1 passes; the flag is not a failure
    assert close.lookahead_gate(c, {})["status"] == "pending_review"
    assert close.lookahead_gate(c, {"f3": "late_news"})["status"] == "pass"
    assert close.lookahead_gate(c, {"f3": "unexplained"})["status"] == "pass"


def test_l2_contamination_fails_the_gate():
    g = close.lookahead_gate(control(flags=(3,)), {"f3": "in_play_contamination"})
    assert g["status"] == "fail" and g["fails"] == ["L2_in_play_contamination"]


def test_l2_flag_rate_above_two_percent_fails_whatever_the_causes():
    c = control(n=500, flags=tuple(range(11)))                     # 11/500 = 2.2%
    g = close.lookahead_gate(c, {f"f{i}": "late_news" for i in range(11)})
    assert g["status"] == "fail" and "L2_flag_rate" in g["fails"]
    c = control(n=500, flags=tuple(range(10)))                     # 10/500 = 2.0%: at the cap, not above
    assert close.lookahead_gate(c, {f"f{i}": "late_news" for i in range(10)})["status"] == "pass"


def test_l2_unknown_cause_refused():
    with pytest.raises(ValueError, match="unknown causes"):
        close.lookahead_gate(control(flags=(3,)), {"f3": "looked fine"})


# --- 30 Oct amendments (before any data) --------------------------------------------------------------

def test_main_line_uses_half_lines_only():
    t60 = iso(CUT - 70 * MIN)
    cat = close.market_catalog([
        {"marketId": 1062, "marketType": "spreads", "period": "fulltime", "handicap": -1.25},
        {"marketId": 1060, "marketType": "spreads", "period": "fulltime", "handicap": -1.5},
        {"marketId": 1068, "marketType": "spreads", "period": "fulltime", "handicap": -0.5},
        {"marketId": 1064, "marketType": "spreads", "period": "fulltime", "handicap": -1.0}])
    body = payload({("1062", "1062"): [(t60, 1.95, True)], ("1062", "1063"): [(t60, 1.95, True)],   # quarter: 50/50
                    ("1064", "1064"): [(t60, 1.96, True)], ("1064", "1065"): [(t60, 1.94, True)],   # whole line
                    ("1060", "1060"): [(t60, 2.30, True)], ("1060", "1061"): [(t60, 1.66, True)],
                    ("1068", "1068"): [(t60, 1.50, True)], ("1068", "1069"): [(t60, 2.70, True)]})
    assert close.main_lines(close.series_from_history(body), CUT, cat) == {"spreads:fulltime": "1060"}


@pytest.mark.parametrize("fam,status", [
    ("1x2:fulltime", "confirmatory"), ("totals:fulltime", "confirmatory"), ("spreads:fulltime", "confirmatory"),
    ("bothteamsscore:fulltime", "confirmatory"), ("totals-corners:fulltime", "confirmatory"),
    ("spreads-corners:fulltime", "confirmatory"), ("correctscore:fulltime", "excluded"),
    ("winningmargin:fulltime", "excluded"), ("spreads-european:fulltime", "excluded"), ("1x2:p1", "excluded"),
    ("totals-bookings:fulltime", "excluded"), ("doublechance:fulltime", "excluded"),
    ("teamtotals-corners-team1:fulltime", "excluded"), ("something-new:fulltime", "excluded")])
def test_family_eligibility_is_explicit(fam, status):
    got, reason = close.family_eligibility(fam)
    assert got == status and reason
