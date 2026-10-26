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

Pre-registered additions (v0.5.2, before any gate data; DESIGN §0.2-§0.3):
  close-v1b (bridged, a SENSITIVITY rule, never silently substituted): when the
    record in force at the cutoff is suspended, use the last ACTIVE record if it
    lies within BRIDGE_MS (5 min) before the cutoff. It is reported next to v1;
    it replaces v1 for a market family only through a new close-rule version,
    by the switching rule in DESIGN §0.2, decided from suspension counts alone.
  start sources: a fixture counts toward G0.5a only with >= 2 start sources.
  main line (totals, spreads): the line whose two sides, de-vigged
    multiplicatively, are closest to 50/50 at cutoff - 60 min.
  look-ahead L2 flags trigger inspection (lookahead_gate), not exclusion.
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
BRIDGE_MS = 300_000                 # close-v1b: last active record at most 5 min before the cutoff
MIN_START_SOURCES_G05A = 2
MAIN_LINE_LEAD_MS = 3_600_000       # main line chosen at cutoff - 60 min, before the L1 windows
L2_JUMP = 0.10                      # look-ahead flag: |p_close - p(cutoff-10)| > 10pp
L2_MAX_FLAG_RATE = 0.02             # gate fails if more than 2% of fixtures with a close are flagged
L2_CAUSES = ("in_play_contamination", "late_news", "data_error", "unexplained")


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

    @property
    def n_sources(self) -> int:
        return len(self.sources)

    @property
    def counts_toward_g05a(self) -> bool:
        """Late-kickoff protection is only tested when two independent start times exist."""
        return self.cutoff_ms is not None and self.n_sources >= MIN_START_SOURCES_G05A


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
    bridged: bool = False


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


def close_series(records: Iterable[Record], cutoff_ms: int, buffer_ms: int = BUFFER_MS,
                 bridge_ms: int | None = None) -> SeriesClose:
    """close-v1 when bridge_ms is None; close-v1b (sensitivity) with bridge_ms = BRIDGE_MS."""
    recs = sorted(records, key=lambda r: r.t_ms)
    before = [r for r in recs if r.t_ms < cutoff_ms]
    n_buf = sum(1 for r in recs if cutoff_ms <= r.t_ms < cutoff_ms + buffer_ms)
    n_after = sum(1 for r in recs if r.t_ms >= cutoff_ms)
    if not before:
        return SeriesClose(None, None, None, "no_record_before_cutoff", 0, n_buf, n_after)
    last = in_force(recs, cutoff_ms)
    if last is None:
        return SeriesClose(None, None, None, "ambiguous_tie_at_last_record", len(before), n_buf, n_after)
    bridged = False
    if not last.active:
        active = [r for r in before if r.active]
        if bridge_ms is None or not active or active[-1].t_ms < cutoff_ms - bridge_ms:
            return SeriesClose(None, last.t_ms, None, "suspended_at_cutoff", len(before), n_buf, n_after)
        tied = {r.price for r in active if r.t_ms == active[-1].t_ms}
        if len(tied) > 1:
            return SeriesClose(None, None, None, "ambiguous_tie_at_last_record", len(before), n_buf, n_after)
        last, bridged = active[-1], True
        before = [r for r in before if r.t_ms <= last.t_ms]
    priced = [r for r in before if r.active] if bridged else before   # v1 diagnostics unchanged
    change = priced[0].t_ms
    for prev, cur in zip(priced, priced[1:]):
        if cur.price != prev.price:
            change = cur.t_ms
    return SeriesClose(last.price, last.t_ms, change, None, len(before), n_buf, n_after, bridged)


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


def market_catalog(body) -> dict[str, dict]:
    """market_id -> {type, period, handicap, name} from an OddsPapi /v4/markets body.
    Tolerant of a bare list or a list nested one level inside a dict."""
    items = body
    if isinstance(body, dict):
        items = next((v for v in body.values() if isinstance(v, list)), [])
    out = {}
    for m in items or []:
        if isinstance(m, dict) and m.get("marketId") is not None:
            h = m.get("handicap")
            out[str(m["marketId"])] = {"type": m.get("marketType"), "period": m.get("period"),
                                       "handicap": float(h) if h not in (None, "") else None,
                                       "name": m.get("marketName")}
    return out


def _is_team_market(player_id: str) -> bool:
    return player_id in ("0", "", "None", "null")


def main_lines(series: dict, cutoff_ms: int, catalog: dict[str, dict]) -> dict[str, str]:
    """family -> main market id. Families with one market (1x2, BTTS) are their own main
    line. For two-way families with several lines (totals, spreads) the main line is the
    one whose sides, de-vigged multiplicatively, are closest to 50/50 at cutoff - 60 min,
    with both sides active then. Ties: smaller |handicap|, then lower market id."""
    t = cutoff_ms - MAIN_LINE_LEAD_MS
    fams: dict[str, dict[str, dict[str, Record | None]]] = {}
    for (m, o, pl), recs in series.items():
        if m not in catalog or not _is_team_market(pl):
            continue
        fam = f"{catalog[m]['type']}:{catalog[m]['period']}"
        fams.setdefault(fam, {}).setdefault(m, {})[o] = in_force(sorted(recs, key=lambda r: r.t_ms), t)
    out = {}
    for fam, markets in fams.items():
        if len(markets) == 1:
            out[fam] = next(iter(markets))
            continue
        best = None
        for m, sides in markets.items():
            if len(sides) != 2 or any(r is None or not r.active for r in sides.values()):
                continue
            inv = [1 / r.price for r in sides.values()]
            dist = abs(inv[0] / sum(inv) - 0.5)
            h = catalog[m]["handicap"]
            key = (round(dist, 9), abs(h) if h is not None else 1e9, int(m) if m.isdigit() else m)
            if best is None or key < best[0]:
                best = (key, m)
        out[fam] = best[1] if best else None
    return out


def fixture_close_report(body: dict, cut: Cutoff, book: str = "pinnacle", catalog: dict | None = None) -> dict:
    """Summary of the close for every series in one fixture's history, under close-v1,
    with the close-v1b sensitivity, per market and (given the /v4/markets catalog) per family."""
    if cut.cutoff_ms is None:
        return {"rule": RULE_VERSION, "excluded": cut.excluded_reason, "basis": cut.basis, "sources": cut.sources,
                "start_sources_present": cut.n_sources, "counts_toward_g05a": False}
    series = series_from_history(body, book)
    closes = {k: close_series(v, cut.cutoff_ms) for k, v in series.items()}
    bridged = {k: close_series(v, cut.cutoff_ms, bridge_ms=BRIDGE_MS) for k, v in series.items()}
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

    by_market: dict[str, dict] = {}
    suspended = []
    for k, c in closes.items():
        m = by_market.setdefault(k[0], {"series": 0, "with_close_v1": 0, "suspended_at_cutoff": 0,
                                        "with_close_v1b": 0})
        m["series"] += 1
        m["with_close_v1"] += c.reason is None
        m["with_close_v1b"] += bridged[k].reason is None
        if c.reason == "suspended_at_cutoff":
            m["suspended_at_cutoff"] += 1
            act = [r.t_ms for r in series[k] if r.active and r.t_ms < cut.cutoff_ms]
            suspended.append({"market": k[0], "outcome": k[1], "player": k[2],
                              "suspended_for_min": round((cut.cutoff_ms - c.record_ms) / 60_000, 1),
                              "last_active_before_cutoff_min": round((cut.cutoff_ms - max(act)) / 60_000, 1)
                              if act else None,
                              "bridged_by_v1b": bridged[k].reason is None})
    if catalog:
        for mid, m in by_market.items():
            info = catalog.get(mid) or {}
            m.update({"type": info.get("type"), "period": info.get("period"), "handicap": info.get("handicap")})

    rep = {
        "rule": RULE_VERSION,
        "basis": cut.basis,
        "sources": cut.sources,
        "start_sources_present": cut.n_sources,
        "counts_toward_g05a": cut.counts_toward_g05a,
        "cutoff_ms": cut.cutoff_ms,
        "series_total": len(series),
        "series_with_close": len(ok),
        "series_without_close": reasons,
        "series_with_close_v1b_sensitivity": sum(1 for c in bridged.values() if c.reason is None),
        "records_total": sum(len(v) for v in series.values()),
        "records_before_cutoff": len(all_before),
        "records_in_buffer": sum(c.n_in_buffer for c in closes.values()),
        "records_at_or_after_cutoff": sum(c.n_after_cutoff for c in closes.values()),
        "book_silence_at_cutoff_min": None if book_last is None else round((cut.cutoff_ms - book_last) / 60_000, 1),
        "close_age_last_record_min": {"median": q(age_rec, 50), "p90": q(age_rec, 90)},
        "close_age_last_change_min": {"median": q(age_chg, 50), "p90": q(age_chg, 90)},
        "by_market": dict(sorted(by_market.items(), key=lambda kv: (len(kv[0]), kv[0]))),
        "suspended_at_cutoff_detail": sorted(suspended, key=lambda d: (d["market"], d["outcome"], d["player"])),
    }
    if catalog:
        mains = main_lines(series, cut.cutoff_ms, catalog)
        fam = {}
        for (mk, o, pl), c in closes.items():
            info = catalog.get(mk)
            if not info or not _is_team_market(pl):
                continue
            f = fam.setdefault(f"{info['type']}:{info['period']}", {
                "main_market": mains.get(f"{info['type']}:{info['period']}"), "markets": set(), "series": 0,
                "with_close_v1": 0, "suspended_at_cutoff": 0, "main_sides": 0, "main_sides_closed_v1": 0,
                "main_sides_closed_v1b": 0, "main_sides_suspended_v1": 0})
            f["markets"].add(mk)
            f["series"] += 1
            f["with_close_v1"] += c.reason is None
            f["suspended_at_cutoff"] += c.reason == "suspended_at_cutoff"
            if mk == f["main_market"]:
                f["main_sides"] += 1
                f["main_sides_closed_v1"] += c.reason is None
                f["main_sides_closed_v1b"] += bridged[(mk, o, pl)].reason is None
                f["main_sides_suspended_v1"] += c.reason == "suspended_at_cutoff"
        for f in fam.values():
            f["markets"] = len(f["markets"])
            f["main_line_usable_v1"] = f["main_sides"] > 0 and f["main_sides_closed_v1"] == f["main_sides"]
            f["main_line_usable_v1b"] = f["main_sides"] > 0 and f["main_sides_closed_v1b"] == f["main_sides"]
        rep["by_family"] = dict(sorted(fam.items()))
        rep["catalog_markets_missing"] = sorted({k[0] for k in series} - set(catalog))
    return rep


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
    # `passed` is L1 only. L2 flags (jumps_over_10pp) go to inspection: lookahead_gate().
    return ControlResult(n, round(statistics.fmean(fin), 3), ci(fin), round(statistics.fmean(prev), 3),
                         round(statistics.fmean(diff), 3), ci_d, jumps, passed=ci_d[1] < 0.5)


def lookahead_gate(ctrl: ControlResult, reviews: dict) -> dict:
    """G0.5d, pre-registered (DESIGN §0.3).

    reviews: fixture_id -> cause, one of L2_CAUSES, recorded after inspecting the
    flagged fixture's record timeline and start sources. Flagged fixtures are never
    dropped. The gate:
      pending  while any flag lacks a recorded cause;
      fails    if L1 fails, or any cause is in_play_contamination, or flags exceed
               L2_MAX_FLAG_RATE of the fixtures in the control;
      passes   otherwise. late_news, data_error and unexplained flags are listed in
               the report but do not fail the gate below the rate cap.
    A contamination finding is fixed by a new close-rule version applied to every
    fixture, never by excluding the flagged ones.
    """
    flags = [fid for fid, _ in ctrl.jumps_over_10pp]
    bad = {fid: c for fid, c in reviews.items() if c not in L2_CAUSES}
    if bad:
        raise ValueError(f"unknown causes {bad}; allowed {L2_CAUSES}")
    unreviewed = [f for f in flags if f not in reviews]
    rate = len(flags) / ctrl.n if ctrl.n else 0.0
    causes: dict[str, int] = {}
    for f in flags:
        if f in reviews:
            causes[reviews[f]] = causes.get(reviews[f], 0) + 1
    fails = []
    if not ctrl.passed:
        fails.append("L1")
    if causes.get("in_play_contamination"):
        fails.append("L2_in_play_contamination")
    if rate > L2_MAX_FLAG_RATE:
        fails.append("L2_flag_rate")
    status = "fail" if fails else ("pending_review" if unreviewed else "pass")
    return {"status": status, "fails": fails, "n": ctrl.n, "flags": len(flags), "flag_rate": round(rate, 4),
            "flag_rate_max": L2_MAX_FLAG_RATE, "causes": causes, "unreviewed": unreviewed}
