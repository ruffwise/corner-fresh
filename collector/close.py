"""The closing-price rule: ONE code path for exploration, confirmation and diagnostics.

Rule close-v1 (pre-registered; mirrored by close_rule in the schema, and
enforced again by the close_cutoff / close_price triggers):

  cutoff = min(sources present) - 2 min, where the sources are
           api-football scheduled kickoff (always), api-football first-half
           start (fixture.periods.first, when present) and OddsPapi
           trueStartTime (when present).
  No close for the fixture if
    - api-football status is not FT/AET/PEN, or
    - the sources present span more than 10 min (they disagree: pick none).
  For each price series (one outcome of one market of one book):
    close = the record IN FORCE at the cutoff, i.e. the last record strictly
            before the cutoff. It must be active; a suspended (inactive) record
            in force means no close. It never falls back to an earlier price.
  Staleness is reported two ways, because same-price records repeat:
    age_last_record = cutoff - time of the last record
    age_last_change = cutoff - time the price last changed
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, NamedTuple

RULE_VERSION = "close-v1"
BUFFER_MS = 120_000
TOLERANCE_MS = 600_000
FINISHED = ("FT", "AET", "PEN")


class Record(NamedTuple):
    t_ms: int
    price: float
    active: bool


@dataclass(frozen=True)
class Cutoff:
    cutoff_ms: int | None
    excluded_reason: str | None
    basis: str
    sources: dict


def iso_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def cutoff(scheduled_ko_ms: int, status_short: str, af_actual_start_ms: int | None = None,
           op_true_start_ms: int | None = None, buffer_ms: int = BUFFER_MS,
           tolerance_ms: int = TOLERANCE_MS) -> Cutoff:
    srcs = {"af_scheduled": scheduled_ko_ms}
    if af_actual_start_ms is not None:
        srcs["af_first_half_start"] = af_actual_start_ms
    if op_true_start_ms is not None:
        srcs["op_true_start"] = op_true_start_ms
    if status_short not in FINISHED:
        return Cutoff(None, "status_not_finished", f"status {status_short}", srcs)
    spread = max(srcs.values()) - min(srcs.values())
    if spread > tolerance_ms:
        return Cutoff(None, "start_sources_disagree", f"sources span {spread // 1000}s > {tolerance_ms // 1000}s", srcs)
    t = min(srcs.values()) - buffer_ms
    return Cutoff(t, None, f"min({', '.join(sorted(srcs))}) - {buffer_ms // 1000}s", srcs)


@dataclass(frozen=True)
class SeriesClose:
    price: float | None
    record_ms: int | None
    last_change_ms: int | None
    reason: str | None
    n_before: int
    n_in_buffer: int
    n_after_cutoff: int


def in_force(records: list[Record], t_ms: int) -> Record | None:
    """The last record strictly before t_ms, from a time-sorted list. Ties at the
    same timestamp with different prices are ambiguous and return None."""
    before = [r for r in records if r.t_ms < t_ms]
    if not before:
        return None
    last_t = before[-1].t_ms
    tied = {(r.price, r.active) for r in before if r.t_ms == last_t}
    if len(tied) > 1:
        return None
    return before[-1]


def close_series(records: Iterable[Record], cutoff_ms: int, buffer_ms: int = BUFFER_MS) -> SeriesClose:
    recs = sorted(records, key=lambda r: r.t_ms)
    before = [r for r in recs if r.t_ms < cutoff_ms]
    n_buf = sum(1 for r in recs if cutoff_ms <= r.t_ms < cutoff_ms + buffer_ms)
    n_after = sum(1 for r in recs if r.t_ms >= cutoff_ms)
    if not before:
        return SeriesClose(None, None, None, "no_record_before_cutoff", 0, n_buf, n_after)
    last = in_force(recs, cutoff_ms)
    if last is None:
        return SeriesClose(None, None, None, "ambiguous_tie_at_last_record", len(before), n_buf, n_after)
    if not last.active:
        return SeriesClose(None, last.t_ms, None, "suspended_at_cutoff", len(before), n_buf, n_after)
    change = before[0].t_ms
    for prev, cur in zip(before, before[1:]):
        if cur.price != prev.price:
            change = cur.t_ms
    return SeriesClose(last.price, last.t_ms, change, None, len(before), n_buf, n_after)


# --- OddsPapi /v4/historical-odds payloads ----------------------------------------------

def series_from_history(body: dict, book: str = "pinnacle") -> dict[tuple[str, str, str], list[Record]]:
    """(market_id, outcome_id, player_id) -> records, from a /v4/historical-odds body."""
    out: dict[tuple[str, str, str], list[Record]] = {}
    books = (body or {}).get("bookmakers") or {}
    for market_id, m in ((books.get(book) or {}).get("markets") or {}).items():
        for outcome_id, o in (m.get("outcomes") or {}).items():
            for player_id, recs in (o.get("players") or {}).items():
                out[(str(market_id), str(outcome_id), str(player_id))] = [
                    Record(iso_ms(r["createdAt"]), float(r["price"]), bool(r.get("active", True)))
                    for r in recs or [] if r.get("createdAt") is not None and r.get("price") is not None]
    return out


def payload_true_start(body: dict) -> int | None:
    for k in ("trueStartTime", "true_start_time"):
        v = (body or {}).get(k)
        if v:
            return iso_ms(v)
    return None


def fixture_close_report(body: dict, cut: Cutoff, book: str = "pinnacle") -> dict:
    """Summary of the close for every series in one fixture's history, under close-v1."""
    if cut.cutoff_ms is None:
        return {"rule": RULE_VERSION, "excluded": cut.excluded_reason, "basis": cut.basis, "sources": cut.sources}
    series = series_from_history(body, book)
    closes = {k: close_series(v, cut.cutoff_ms) for k, v in series.items()}
    all_before = [r.t_ms for v in series.values() for r in v if r.t_ms < cut.cutoff_ms]
    book_last = max(all_before) if all_before else None
    ok = [c for c in closes.values() if c.reason is None]
    reasons: dict[str, int] = {}
    for c in closes.values():
        if c.reason:
            reasons[c.reason] = reasons.get(c.reason, 0) + 1
    age_rec = [(cut.cutoff_ms - c.record_ms) / 60_000 for c in ok]
    age_chg = [(cut.cutoff_ms - c.last_change_ms) / 60_000 for c in ok]
    q = lambda xs, p: round(statistics.quantiles(xs, n=100)[p - 1], 1) if len(xs) >= 2 else (round(xs[0], 1) if xs else None)  # noqa: E731
    return {
        "rule": RULE_VERSION,
        "basis": cut.basis,
        "sources": cut.sources,
        "cutoff_ms": cut.cutoff_ms,
        "series_total": len(series),
        "series_with_close": len(ok),
        "series_without_close": reasons,
        "records_total": sum(len(v) for v in series.values()),
        "records_before_cutoff": len(all_before),
        "records_in_buffer": sum(c.n_in_buffer for c in closes.values()),
        "records_at_or_after_cutoff": sum(c.n_after_cutoff for c in closes.values()),
        "book_silence_at_cutoff_min": None if book_last is None else round((cut.cutoff_ms - book_last) / 60_000, 1),
        "close_age_last_record_min": {"median": q(age_rec, 50), "p90": q(age_rec, 90)},
        "close_age_last_change_min": {"median": q(age_chg, 50), "p90": q(age_chg, 90)},
    }


# --- Look-ahead control ---------------------------------------------------------------------

@dataclass
class ControlResult:
    n: int
    mean_final_pp: float
    ci_final_pp: tuple[float, float]
    mean_prev_pp: float
    mean_diff_pp: float
    ci_diff_pp: tuple[float, float]
    jumps_over_10pp: list = field(default_factory=list)
    passed: bool = False


def lookahead_control(rows: list[tuple[str, float, float, float, int]], boot: int = 10_000,
                      seed: int = 20261025) -> ControlResult:
    """rows: (fixture_id, p_cut_minus_20, p_cut_minus_10, p_close, y) with p the implied
    probability (1/price) of ONE pre-registered selection per fixture and y its outcome.

    signed move = (p_later - p_earlier) * (2y - 1): positive when the price moved
    towards what happened. An in-play record leaking into the close shows up as
    a large positive final-window move (a goal moves a price by tens of points).

    The literal test "final-window signed move = 0" would also flag ordinary
    informative late money, which is positive by construction. So the gate
    (pre-registered) compares the final window with the window before it:
      L1: upper 95% CI of mean(final - previous) < 0.5 pp
      L2: no fixture with |p_close - p_cut_minus_10| > 10 pp
    The final-window mean and its CI are still reported as the plain test.
    """
    import random
    rnd = random.Random(seed)
    fin = [(c - m10) * (2 * y - 1) * 100 for _, m20, m10, c, y in rows]
    prev = [(m10 - m20) * (2 * y - 1) * 100 for _, m20, m10, c, y in rows]
    diff = [a - b for a, b in zip(fin, prev)]
    n = len(rows)
    if n == 0:
        return ControlResult(0, float("nan"), (float("nan"),) * 2, float("nan"), float("nan"), (float("nan"),) * 2)

    def ci(xs):
        means = sorted(statistics.fmean(rnd.choices(xs, k=len(xs))) for _ in range(boot))
        return round(means[int(0.025 * boot)], 3), round(means[int(0.975 * boot) - 1], 3)
    jumps = [(fid, round((c - m10) * 100, 1)) for fid, m20, m10, c, y in rows if abs(c - m10) > 0.10]
    ci_d = ci(diff)
    return ControlResult(n, round(statistics.fmean(fin), 3), ci(fin), round(statistics.fmean(prev), 3),
                         round(statistics.fmean(diff), 3), ci_d, jumps, passed=(ci_d[1] < 0.5 and not jumps))
