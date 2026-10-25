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
