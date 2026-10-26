"""Expected-slots ledger: planner, slot claim/close, watchdog."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from .store import now_ms

HOUR = 3_600_000
# Nominal cadence per scheduled job (ms) and the offset within the period.
# These must match the systemd timers in deploy/ (checked by a test).
CADENCE = {
    "af_fixtures": (6 * HOUR, 15 * 60_000),     # 00:15, 06:15, 12:15, 18:15 UTC
    "af_odds": (HOUR, 5 * 60_000),              # hh:05
    "af_results": (HOUR, 35 * 60_000),          # hh:35
    "op_account": (24 * HOUR, 50 * 60_000),     # 00:50 daily (free)
    "op_fixtures": (24 * HOUR, 55 * 60_000),    # 00:55 daily; spends only when a refresh is due
    "op_history": (HOUR, 45 * 60_000),          # hh:45 (free)
}
GRACE_MS = 15 * 60_000


def nominal(job: str, t_ms: int) -> int:
    period, offset = CADENCE[job]
    return ((t_ms - offset) // period) * period + offset


def _check_jobs(jobs) -> tuple[str, ...]:
    jobs = tuple(jobs)
    unknown = sorted(set(jobs) - set(CADENCE))
    if unknown:
        raise ValueError(f"not scheduled jobs: {unknown}")
    return jobs


def plan(con: sqlite3.Connection, jobs, horizon_h: int = 48, t: int | None = None) -> dict:
    """Plan slots for the ACTIVE jobs only (config `active_jobs`).

    Pending slots of jobs that are not active are removed: they are plans that
    will never run, and left in place they become false misses. A pending slot
    has no requests (requests are created only after a slot is claimed), so
    removing it loses no observation. Claimed, complete, failed and missed
    slots are never touched.
    """
    jobs = _check_jobs(jobs)
    t = now_ms() if t is None else t
    n = 0
    con.execute("BEGIN")
    for job in jobs:
        period, _ = CADENCE[job]
        s = nominal(job, t) + period
        while s <= t + horizon_h * HOUR:
            n += con.execute("INSERT OR IGNORE INTO capture_slot (job, scheduled_for_ms) VALUES (?, ?)",
                             (job, s)).rowcount
            s += period
    inactive = [j for j in CADENCE if j not in jobs]
    removed = {}
    for job in inactive:
        k = con.execute("DELETE FROM capture_slot WHERE job = ? AND status = 'pending' AND NOT EXISTS"
                        " (SELECT 1 FROM api_request a WHERE a.slot_id = capture_slot.slot_id)", (job,)).rowcount
        if k:
            removed[job] = k
    con.execute("COMMIT")
    return {"slots_planned": n, "active_jobs": list(jobs), "pending_removed_for_inactive_jobs": removed}


INACTIVE_TIMER_NOTE = "timer fired but job is not in active_jobs: refused"


def watchdog(con: sqlite3.Connection, jobs, t: int | None = None) -> list[tuple[str, int]]:
    """Mark pending slots of ACTIVE jobs past their grace period as missed; return them.
    Also reports an absent plan (no future slot for an active job), and a timer that
    fired for a job outside active_jobs in the last 2 h (config and systemd disagree).
    The 2 h window makes each drift event fail the watchdog briefly instead of masking
    every other failure for a day; a timer left enabled keeps firing and keeps failing it."""
    jobs = _check_jobs(jobs)
    t = now_ms() if t is None else t
    marks = ",".join("?" * len(jobs)) or "NULL"
    missed = con.execute(f"SELECT job, scheduled_for_ms FROM capture_slot WHERE status='pending'"
                         f" AND scheduled_for_ms + ? < ? AND job IN ({marks}) ORDER BY scheduled_for_ms",
                         (GRACE_MS, t, *jobs)).fetchall()
    con.execute(f"UPDATE capture_slot SET status='missed', note='watchdog: not started within grace'"
                f" WHERE status='pending' AND scheduled_for_ms + ? < ? AND job IN ({marks})", (GRACE_MS, t, *jobs))
    for job in jobs:
        if not con.execute("SELECT 1 FROM capture_slot WHERE job=? AND scheduled_for_ms > ?", (job, t)).fetchone():
            missed.append((f"{job}:no_future_slot_planned", t))
    for job, s_for in con.execute("SELECT job, scheduled_for_ms FROM capture_slot WHERE note = ?"
                                  " AND started_at_ms > ?", (INACTIVE_TIMER_NOTE, t - 2 * HOUR)):
        missed.append((f"{job}:timer_enabled_but_not_in_active_jobs", s_for))
    return missed


def refuse_inactive_timer_run(con: sqlite3.Connection, job: str) -> None:
    """A timer fired for a job that active_jobs does not list. Record it as a failed
    slot (so the watchdog reports the drift) and do no work."""
    t = now_ms()
    s_for = nominal(job, t)
    row = con.execute("SELECT slot_id, status FROM capture_slot WHERE job=? AND scheduled_for_ms=?",
                      (job, s_for)).fetchone()
    if row is None:
        con.execute("INSERT INTO capture_slot (job, scheduled_for_ms, status, started_at_ms, note)"
                    " VALUES (?,?,'failed',?,?)", (job, s_for, t, INACTIVE_TIMER_NOTE))
    elif row[1] == "pending":
        con.execute("UPDATE capture_slot SET status='failed', started_at_ms=?, note=? WHERE slot_id=?",
                    (t, INACTIVE_TIMER_NOTE, row[0]))


class SlotIncomplete(RuntimeError):
    """The run finished but did not complete every expected request."""


class Slot:
    def __init__(self, con: sqlite3.Connection, slot_id: int):
        self.con = con
        self.slot_id = slot_id
        self.expected = 0
        self.basis: list[str] = []

    def expect(self, n: int, basis: str) -> None:
        self.expected += n
        self.basis.append(basis)


@contextmanager
def run_slot(con: sqlite3.Connection, job: str, scheduled_for: int | None = None, unplanned_ok: bool = False):
    """Claim the slot for this run, yield it, then close it.

    Closing as 'complete' goes through the schema trigger, which refuses if
    completed requests != expected requests. Any exception marks it failed.
    """
    t = now_ms()
    s_for = scheduled_for if scheduled_for is not None else (nominal(job, t) if job in CADENCE else t)
    row = con.execute("SELECT slot_id, status FROM capture_slot WHERE job=? AND scheduled_for_ms=?",
                      (job, s_for)).fetchone()
    if row is None:
        slot_id = con.execute(
            "INSERT INTO capture_slot (job, scheduled_for_ms, status, started_at_ms, note) VALUES (?,?,'running',?,?)",
            (job, s_for, t, None if unplanned_ok else "unplanned: no pending slot existed")).lastrowid
    else:
        slot_id, status = row
        if status not in ("pending", "missed"):
            raise RuntimeError(f"slot {job}@{s_for} already {status}")
        late = "late start after watchdog marked missed" if status == "missed" else None
        con.execute("UPDATE capture_slot SET status='running', started_at_ms=?, note=? WHERE slot_id=?",
                    (t, late, slot_id))
    slot = Slot(con, slot_id)
    try:
        yield slot
    except BaseException as e:
        con.execute("UPDATE capture_slot SET status='failed', note=? WHERE slot_id=?",
                    (f"{type(e).__name__}: {e}"[:500], slot_id))
        raise
    basis = "; ".join(slot.basis) or "no work derived"
    note = None if slot.expected else "no work in window"
    con.execute("UPDATE capture_slot SET expected_requests=?, expected_basis=?, note=coalesce(note, ?)"
                " WHERE slot_id=?", (slot.expected, basis, note, slot_id))
    try:
        con.execute("UPDATE capture_slot SET status='complete', completed_at_ms=? WHERE slot_id=?",
                    (now_ms(), slot_id))
    except sqlite3.IntegrityError as e:
        con.execute("UPDATE capture_slot SET status='failed', note=? WHERE slot_id=?", (str(e)[:500], slot_id))
        raise SlotIncomplete(f"{job}: {e}") from None
