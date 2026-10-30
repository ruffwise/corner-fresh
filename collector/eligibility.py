"""Fixture eligibility rules fixed before data (DESIGN §9.1). Analysis code (M7/M8)
must call these; nothing decides eligibility inline.

UEFA club competitions (api-football league 2 = Champions League, 3 = Europa
League): qualifying rounds don't count. A fixture counts only if it kicks off on or
after the first kickoff of a round named 'League Stage...' in the same league-season.
The rule is defined on the main-competition start, not on round names, so every later
round counts whatever it is called. api-football's names (checked on the VM, 30 Oct,
both leagues, 2025 and 2026): '1st/2nd/3rd Qualifying Round', 'Play-offs' (August),
'League Stage - 1'..'- 8', then 'Round of 32', 'Round of 16', 'Quarter-finals',
'Semi-finals', 'Final'.
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


# --- G0.5a and settled no_history (pre-registered 30 Oct, before any G0.5 data) -------------------

def history_window_start_ms(con: sqlite3.Connection) -> int | None:
    """Start time of the earliest fixture with a usable OddsPapi history response: the
    first available history day, measured, not assumed (the 30 Oct probe put it
    between 4 and 16 Jan 2026)."""
    row = con.execute(
        "SELECT min(c.start_ms) FROM provider_fixture_candidate c WHERE c.provider = 'oddspapi' AND EXISTS"
        " (SELECT 1 FROM raw_response r JOIN api_request a ON a.request_id = r.request_id"
        "  WHERE a.provider = 'oddspapi' AND a.endpoint = '/v4/historical-odds' AND r.usable = 1"
        "    AND a.params_canonical LIKE '%fixtureId=' || c.provider_fixture_id)").fetchone()
    return row[0] if row else None


def g05a_history_status(start_ms: int, has_history: bool, settled_no_history: bool,
                        window_start_ms: int | None) -> str:
    """How a mapped fixture enters the G0.5a denominator:
      'counted'      - usable history: judged by the close-v1 rules;
      'unusable'     - settled no_history on or after the first available history day:
                       stays IN the denominator as unusable, so vendor gaps can't hide;
      'out_of_window'- before the first available history day (exploration only):
                       excluded from G0.5a and reported;
      'pending'      - not yet asked or answered: not counted until settled."""
    if has_history:
        return "counted"
    if window_start_ms is not None and start_ms < window_start_ms:
        return "out_of_window"
    return "unusable" if settled_no_history else "pending"
