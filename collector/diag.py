"""Diagnostics on raw payloads that do not need reviewed allowlists.

These read the archive directly and print facts; they never write parsed
prices. Used for the day-1 checks (G0.5 evidence on a known fixture).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone


def iso_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


def history_records(body: dict, book: str = "pinnacle"):
    """Yield (market_id, outcome_id, player_id, record) from a /v4/historical-odds body."""
    books = (body or {}).get("bookmakers") or {}
    for market_id, m in ((books.get(book) or {}).get("markets") or {}).items():
        for outcome_id, o in (m.get("outcomes") or {}).items():
            for player_id, recs in (o.get("players") or {}).items():
                for r in recs or []:
                    yield market_id, outcome_id, player_id, r


def close_window_report(body: dict, kickoff_ms: int, book: str = "pinnacle", window_min: int = 60) -> dict:
    lo = kickoff_ms - window_min * 60_000
    per_market = Counter()
    per_market_total = Counter()
    last_before_ko = None
    after_ko = 0
    gaps = []
    times = []
    for market_id, outcome_id, _, r in history_records(body, book):
        t = iso_ms(r["createdAt"])
        per_market_total[market_id] += 1
        if t < kickoff_ms:
            times.append(t)
            last_before_ko = t if last_before_ko is None else max(last_before_ko, t)
            if t >= lo:
                per_market[market_id] += 1
        else:
            after_ko += 1
    times.sort()
    pre_window = [t for t in times if t >= lo]
    for a, b in zip(pre_window, pre_window[1:]):
        gaps.append(b - a)
    return {
        "book": book,
        "kickoff_utc": datetime.fromtimestamp(kickoff_ms / 1000, tz=timezone.utc).isoformat(),
        "markets_with_any_record": len(per_market_total),
        "records_total": sum(per_market_total.values()),
        f"records_in_final_{window_min}_min": sum(per_market.values()),
        f"markets_with_a_record_in_final_{window_min}_min": len(per_market),
        "last_record_before_kickoff_min": None if last_before_ko is None else round((kickoff_ms - last_before_ko) / 60_000, 1),
        "records_at_or_after_kickoff": after_ko,
        "largest_gap_in_window_min": round(max(gaps) / 60_000, 1) if gaps else None,
        "top_markets_in_window": per_market.most_common(10),
    }


def bookmaker_check(names: list[tuple[int, str]]) -> str:
    exact = [n for n in names if n[1] == "Pinnacle"]
    like = [n for n in names if "pinnacle" in n[1].lower()]
    if len(exact) == 1:
        return f"PASS: exactly one bookmaker named 'Pinnacle' (id {exact[0][0]})"
    if not like:
        return "FAIL: no Pinnacle in /odds/bookmakers. api-football cannot supply Pinnacle snapshots."
    return f"REVIEW: no single exact 'Pinnacle'; candidates {like}. Decide by hand and record the commit."


def summarise_candidates(rows) -> dict:
    by_status = defaultdict(int)
    for status, n in rows:
        by_status[status] += n
    return dict(by_status)
