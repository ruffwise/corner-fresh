"""OddsPapi jobs: account (free), catalog and fixtures (billable, budgeted), history (free).

Quota policy (requirement 9): the 250-request limit may be a lifetime limit.
Every billable call must pass QuotaGuard.authorise(), which
  1. requires a fresh /v4/account observation (it fetches one; that call is free),
  2. refuses if provider count + n would come within `reserve` of the limit,
  3. refuses if OUR billable calls this UTC month + n would exceed our budget.
If the account call fails or its fields are missing, the guard refuses: an
absent quota reading is treated as "no budget", never as "unlimited".
Billable calls are never retried automatically.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .config import Config
from .http import Fetcher, Result
from .ledger import Slot
from .store import now_ms

PROVIDER = "oddspapi"
COOLDOWN_S = {  # documented cooldowns plus a 20% margin
    "/v4/historical-odds": 6.0, "/v4/fixtures": 2.4, "/v4/tournaments": 1.2,
    "/v4/markets": 1.2, "/v4/bookmakers": 1.2, "/v4/account": 1.2,
}
FINISHED_STATUS_KEYS = {"2"}   # OddsPapi statusId 2 = finished (per GET fixtures docs)


class QuotaRefused(RuntimeError):
    pass


def _find(obj, key):
    """Depth-first search for a key in nested JSON (account payload shape is not fully documented)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, key)
            if r is not None:
                return r
    return None


def month_start_ms(t: int) -> int:
    d = datetime.fromtimestamp(t / 1000, tz=timezone.utc)
    return int(datetime(d.year, d.month, 1, tzinfo=timezone.utc).timestamp() * 1000)


class OP:
    def __init__(self, cfg: Config, con: sqlite3.Connection, fetcher: Fetcher):
        if not cfg.op_key:
            raise RuntimeError("OP_KEY is not set")
        self.cfg, self.con, self.f = cfg, con, fetcher

    # --- transport -----------------------------------------------------------------------
    def _get(self, slot: Slot, endpoint: str, params: dict, basis: str, attempts: int = 1) -> Result | None:
        slot.expect(1, basis)
        req_id = self.f.request_row(slot.slot_id, PROVIDER, endpoint, params)
        res = None
        for attempt in range(1, attempts + 1):
            self.f.pace(PROVIDER + endpoint, COOLDOWN_S.get(endpoint, 2.4))
            res = self.f.get(request_id=req_id, provider=PROVIDER, paged=False, url=self.cfg.op_base + endpoint,
                             params=params, secret_params={"apiKey": self.cfg.op_key}, attempt=attempt)
            if res.usable:
                break
            if res.status == 429:
                self.f.sleep(COOLDOWN_S.get(endpoint, 2.4) * 2)
        if res is not None and res.usable:
            self.con.execute("UPDATE api_request SET expected_pages=1 WHERE request_id=?", (req_id,))
            self.con.execute("UPDATE api_request SET status='complete' WHERE request_id=?", (req_id,))
            return res
        self.con.execute("UPDATE api_request SET status='failed' WHERE request_id=?", (req_id,))
        return None

    # --- quota ----------------------------------------------------------------------------
    def account(self, slot: Slot) -> dict:
        res = self._get(slot, "/v4/account", {}, "quota check (free)", attempts=2)
        if res is None:
            raise QuotaRefused("/v4/account failed: no quota reading, refusing billable calls")
        limit, count = _find(res.body, "request_limit"), _find(res.body, "request_count")
        if not (isinstance(limit, int) and isinstance(count, int)):
            raise QuotaRefused(f"/v4/account lacks integer request_limit/request_count: {str(res.body)[:300]}")
        self.con.execute("INSERT INTO quota_obs VALUES (?,?,?,?,?)", (res.raw_id, PROVIDER, limit, count, now_ms()))
        return {"request_limit": limit, "request_count": count, "raw": res.body}

    def our_billable_this_month(self, t: int) -> int:
        return self.con.execute(
            "SELECT count(*) FROM raw_response r JOIN api_request a ON a.request_id = r.request_id"
            " JOIN endpoint_allowlist e ON e.provider = a.provider AND e.endpoint = a.endpoint"
            " WHERE a.provider = ? AND e.billable = 1 AND r.http_status > 0 AND r.requested_at_ms >= ?",
            (PROVIDER, month_start_ms(t))).fetchone()[0]

    def authorise(self, slot: Slot, n: int, why: str) -> None:
        acct = self.account(slot)
        headroom = acct["request_limit"] - self.cfg.op_quota_reserve - acct["request_count"]
        if n > headroom:
            raise QuotaRefused(f"{why}: needs {n}, provider headroom {headroom} "
                               f"(limit {acct['request_limit']}, used {acct['request_count']}, reserve {self.cfg.op_quota_reserve})")
        ours = self.our_billable_this_month(now_ms())
        if ours + n > self.cfg.op_monthly_billable_budget:
            raise QuotaRefused(f"{why}: our monthly budget {self.cfg.op_monthly_billable_budget} would be exceeded "
                               f"({ours} used this month)")

    # --- catalog (manual, one-off) -----------------------------------------------------------
    def catalog(self, slot: Slot, sport_id: int) -> dict:
        self.authorise(slot, 2, "catalog")
        out = {}
        t = self._get(slot, "/v4/tournaments", {"sportId": sport_id}, "tournament catalog (billable)")
        out["tournaments"] = t.body if t else None
        m = self._get(slot, "/v4/markets", {"sportId": sport_id}, "market catalog (billable)")
        out["markets_raw_id"] = m.raw_id if m else None
        return out

    # --- fixtures (billable, refreshed rarely) -------------------------------------------------
    def fixtures_due(self, t: int) -> list[int]:
        due = []
        for lg in self.cfg.leagues:
            if lg.oddspapi_tournament_id is None:
                continue
            last = self.con.execute(
                "SELECT max(r.received_at_ms) FROM raw_response r JOIN api_request a ON a.request_id = r.request_id"
                " WHERE a.provider=? AND a.endpoint='/v4/fixtures' AND r.usable=1"
                " AND (a.params_canonical = ? OR a.params_canonical LIKE ?)",
                (PROVIDER, f"tournamentId={lg.oddspapi_tournament_id}",
                 f"%&tournamentId={lg.oddspapi_tournament_id}")).fetchone()[0]
            if last is None or t - last > self.cfg.op_fixtures_refresh_days * 86_400_000:
                due.append(lg.oddspapi_tournament_id)
        return sorted(set(due))

    def fixtures(self, slot: Slot, force: list[int] | None = None) -> dict:
        due = force if force is not None else self.fixtures_due(now_ms())
        if not due:
            return {"due": [], "listed": 0}
        self.authorise(slot, len(due), f"fixture listing for {len(due)} tournaments")
        listed = 0
        for tid in due:
            params = {"tournamentId": tid}
            if self.cfg.op_fixtures_from:
                params["from"] = self.cfg.op_fixtures_from
            res = self._get(slot, "/v4/fixtures", params, f"fixtures tournament {tid} (billable)")
            if res is not None:
                listed += self.record_candidates(res, tid)
        return {"due": due, "listed": listed}

    def record_candidates(self, res: Result, tid: int) -> int:
        items = res.body if isinstance(res.body, list) else (res.body or {}).get("data", [])
        n = 0
        for it in items:
            try:
                start = int(datetime.fromisoformat(it["startTime"].replace("Z", "+00:00")).timestamp() * 1000)
                row = (PROVIDER, str(it["fixtureId"]), res.raw_id, str(it.get("tournamentId", tid)),
                       str(it["participant1Id"]), str(it["participant2Id"]), it["participant1Name"],
                       it["participant2Name"], start, None if it.get("statusId") is None else str(it["statusId"]))
            except (KeyError, TypeError, ValueError):
                continue   # shape drift: left in the raw archive for review
            n += self.con.execute("INSERT OR IGNORE INTO provider_fixture_candidate VALUES (?,?,?,?,?,?,?,?,?,?)",
                                  row).rowcount
        return n

    # --- history (free) ----------------------------------------------------------------------------
    def history_due(self, t: int) -> list[str]:
        """Latest candidate row per provider fixture id; due once its start is old enough
        and no usable history response exists for it yet."""
        cutoff = t - int(self.cfg.op_history_delay_h * 3_600_000)
        rows = self.con.execute(
            "WITH latest AS (SELECT provider_fixture_id, start_ms, status_key,"
            "  row_number() OVER (PARTITION BY provider_fixture_id ORDER BY raw_id DESC) AS rn"
            "  FROM provider_fixture_candidate WHERE provider = ?)"
            " SELECT provider_fixture_id FROM latest WHERE rn = 1 AND start_ms <= ? AND start_ms >= ?"
            " AND coalesce(status_key, '') <> '3'"
            " AND NOT EXISTS (SELECT 1 FROM raw_response r JOIN api_request a ON a.request_id = r.request_id"
            "   WHERE a.provider = ? AND a.endpoint = '/v4/historical-odds' AND r.usable = 1"
            "     AND a.params_canonical = 'bookmakers=' || ? || '&fixtureId=' || latest.provider_fixture_id)"
            " ORDER BY start_ms LIMIT ?",
            (PROVIDER, cutoff, int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000),
             PROVIDER, self.cfg.op_bookmakers, self.cfg.op_history_max_per_run)).fetchall()
        return [r[0] for r in rows]

    def history(self, slot: Slot, ids: list[str] | None = None) -> dict:
        ids = self.history_due(now_ms()) if ids is None else ids
        ok = 0
        for fid in ids:
            res = self._get(slot, "/v4/historical-odds", {"fixtureId": fid, "bookmakers": self.cfg.op_bookmakers},
                            f"history {fid} (free)", attempts=3)
            ok += res is not None
        return {"requested": len(ids), "usable": ok}
