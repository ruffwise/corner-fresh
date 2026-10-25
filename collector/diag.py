"""Diagnostics that do not need reviewed allowlists.

The closing-price rule lives only in collector/close.py (one code path);
nothing here computes a close.
"""
from __future__ import annotations

from collections import defaultdict


def bookmaker_check(names: list[tuple[int, str | None]]) -> str:
    """names may contain None or empty names: the live feed does (found 2026-10-25)."""
    exact = [n for n in names if n[1] == "Pinnacle"]
    like = [n for n in names if "pinnacle" in (n[1] or "").lower()]
    if len(exact) == 1:
        return f"PASS: exactly one bookmaker named 'Pinnacle' (id {exact[0][0]})"
    if not like:
        return "FAIL: no Pinnacle in /odds/bookmakers. api-football cannot supply Pinnacle snapshots."
    return f"REVIEW: no single exact 'Pinnacle'; candidates {like}. Decide by hand and record the commit."


def unnamed_bookmakers(names: list[tuple[int, str | None]]) -> list[int]:
    return sorted(i for i, n in names if not (n or "").strip())


def summarise_candidates(rows) -> dict:
    by_status = defaultdict(int)
    for status, n in rows:
        by_status[status] += n
    return dict(by_status)
