"""Fixture eligibility rules fixed before data (DESIGN §9.1). Analysis code (M7/M8)
must call these; nothing decides eligibility inline.

UEFA club competitions (api-football league 2 = Champions League, 3 = Europa
League): qualifying rounds don't count. A fixture counts only if it kicks off on or
after the first kickoff of a round named 'League Stage...' in the same league-season.
The rule is defined on the main-competition start, not on qualifying-round names, so
the later 'Knockout Round Play-offs' count while the August 'Play-offs' don't.
"""
from __future__ import annotations

import sqlite3

UEFA_CLUB_LEAGUES = (2, 3)
MAIN_STAGE_PREFIX = "League Stage"


def main_stage_start_ms(con: sqlite3.Connection, league_id: int, season: int) -> int | None:
    row = con.execute(
        "SELECT min(v.kickoff_ms) FROM fixture f JOIN v_fixture_latest v ON v.fixture_id = f.fixture_id"
        " WHERE f.league_id = ? AND f.season = ? AND f.round LIKE ? || '%'",
        (league_id, season, MAIN_STAGE_PREFIX)).fetchone()
    return row[0] if row else None


def fixture_counts(con: sqlite3.Connection, fixture_id: int) -> tuple[bool, str]:
    """(counts, reason) for the competition-stage rule."""
    row = con.execute("SELECT f.league_id, f.season, v.kickoff_ms FROM fixture f"
                      " JOIN v_fixture_latest v ON v.fixture_id = f.fixture_id WHERE f.fixture_id = ?",
                      (fixture_id,)).fetchone()
    if row is None:
        return False, "unknown fixture"
    league, season, ko = row
    if league not in UEFA_CLUB_LEAGUES:
        return True, "domestic league"
    start = main_stage_start_ms(con, league, season)
    if start is None:
        return False, "main-stage start unknown for this league-season: no 'League Stage' round seen yet"
    return (ko >= start, "main competition" if ko >= start else "qualifying round (before the league stage)")
