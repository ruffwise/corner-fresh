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


def plan(con: sqlite3.Connection, horizon_h: int = 48, t: int | None = None) -> int:
    t = now_ms() if t is None else t
    n = 0
    con.execute("BEGIN")
    for job, (period, _) in CADENCE.items():
        s = nominal(job, t) + period
        while s <= t + horizon_h * HOUR:
            n += con.execute("INSERT OR IGNORE INTO capture_slot (job, scheduled_for_ms) VALUES (?, ?)",
                             (job, s)).rowcount
            s += period
    con.execute("COMMIT")
    return n


def watchdog(con: sqlite3.Connection, t: int | None = None) -> list[tuple[str, int]]:
    """Mark pending slots past their grace period as missed; return them.
    Also reports an absent plan: no future slot for a scheduled job."""
    t = now_ms() if t is None else t
    missed = con.execute("SELECT job, scheduled_for_ms FROM capture_slot WHERE status='pending'"
                         " AND scheduled_for_ms + ? < ?", (GRACE_MS, t)).fetchall()
    con.execute("UPDATE capture_slot SET status='missed', note='watchdog: not started within grace'"
                " WHERE status='pending' AND scheduled_for_ms + ? < ?", (GRACE_MS, t))
    for job in CADENCE:
        if not con.execute("SELECT 1 FROM capture_slot WHERE job=? AND scheduled_for_ms > ?", (job, t)).fetchone():
            missed.append((f"{job}:no_future_slot_planned", t))
    return missed


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
