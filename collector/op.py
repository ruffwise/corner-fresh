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
HISTORY_FROM_MS = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)   # documented start of history


def iso_ms(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)


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
    def _call(self, slot: Slot, endpoint: str, params: dict, basis: str, attempts: int = 1,
              terminal: tuple[int, ...] = ()) -> tuple[Result | None, int, int | None]:
        """One logical request: (usable result or None, request_id, last HTTP status).

        Retries only what can succeed on retry: 429 (with exponential back-off on top
        of the endpoint pacing), 5xx and network errors. A status in `terminal` (404
        for history) is a definite answer and is never retried.
        """
        slot.expect(1, basis)
        req_id = self.f.request_row(slot.slot_id, PROVIDER, endpoint, params)
        res = None
        pause = COOLDOWN_S.get(endpoint, 2.4)
        for attempt in range(1, attempts + 1):
            self.f.pace(PROVIDER + endpoint, pause)
            res = self.f.get(request_id=req_id, provider=PROVIDER, paged=False, url=self.cfg.op_base + endpoint,
                             params=params, secret_params={"apiKey": self.cfg.op_key}, attempt=attempt)
            if res.usable or res.status in terminal:
                break
            if res.status == 429 and attempt < attempts:
                self.f.sleep(pause * 2 ** attempt)          # 12 s, then 24 s
        if res is not None and res.usable:
            self.con.execute("UPDATE api_request SET expected_pages=1 WHERE request_id=?", (req_id,))
            self.con.execute("UPDATE api_request SET status='complete' WHERE request_id=?", (req_id,))
            return res, req_id, res.status
        self.con.execute("UPDATE api_request SET status='failed' WHERE request_id=?", (req_id,))
        return None, req_id, (res.status if res is not None else None)

    def _get(self, slot: Slot, endpoint: str, params: dict, basis: str, attempts: int = 1) -> Result | None:
        return self._call(slot, endpoint, params, basis, attempts)[0]

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
    def last_listing_ms(self, tid: int) -> int | None:
        return self.con.execute(
            "SELECT max(r.received_at_ms) FROM raw_response r JOIN api_request a ON a.request_id = r.request_id"
            " WHERE a.provider=? AND a.endpoint='/v4/fixtures' AND r.usable=1"
            " AND (a.params_canonical = ? OR a.params_canonical LIKE ?)",
            (PROVIDER, f"tournamentId={tid}", f"%&tournamentId={tid}")).fetchone()[0]

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
                start = iso_ms(it["startTime"])
                true_start = iso_ms(it["trueStartTime"]) if it.get("trueStartTime") else None
                row = (PROVIDER, str(it["fixtureId"]), res.raw_id, str(it.get("tournamentId", tid)),
                       str(it["participant1Id"]), str(it["participant2Id"]), it["participant1Name"],
                       it["participant2Name"], start, None if it.get("statusId") is None else str(it["statusId"]),
                       true_start)
            except (KeyError, TypeError, ValueError):
                continue   # shape drift: left in the raw archive for review
            n += self.con.execute(
                "INSERT OR IGNORE INTO provider_fixture_candidate (provider, provider_fixture_id, raw_id, tournament_key,"
                " home_team_key, away_team_key, home_team_name, away_team_name, start_ms, status_key, true_start_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)", row).rowcount
        return n

    # --- history (free) ----------------------------------------------------------------------------
    def _history_params(self, fid: str) -> dict:
        return {"fixtureId": fid, "bookmakers": self.cfg.op_bookmakers}

    def history_due(self, t: int, limit: int | None = None) -> list[str]:
        """MAPPED fixtures only (a live, non-revoked provider_fixture_map row): history for
        a fixture we can't join to an api-football fixture and outcome is unusable, so it
        is never fetched. Due once the start is op_history_delay_h old, while there is no
        usable history response, and unless it is settled as no_history: a 404 is asked
        again only after op_history_404_recheck_h, and never after op_history_404_max."""
        cutoff = t - int(self.cfg.op_history_delay_h * 3_600_000)
        recheck = t - int(self.cfg.op_history_404_recheck_h * 3_600_000)
        rows = self.con.execute(
            "WITH latest AS (SELECT provider_fixture_id, start_ms, status_key,"
            "  row_number() OVER (PARTITION BY provider_fixture_id ORDER BY raw_id DESC) AS rn"
            "  FROM provider_fixture_candidate WHERE provider = ?),"
            " nohist AS (SELECT a.params_canonical AS p, count(*) AS n, max(o.decided_at_ms) AS last_ms"
            "  FROM request_outcome o JOIN api_request a ON a.request_id = o.request_id"
            "  WHERE a.provider = ? AND a.endpoint = '/v4/historical-odds' GROUP BY a.params_canonical)"
            " SELECT l.provider_fixture_id FROM latest l"
            " JOIN provider_fixture_map m ON m.provider = ? AND m.provider_fixture_id = l.provider_fixture_id"
            "  AND m.revoked = 0"
            " LEFT JOIN nohist h ON h.p = 'bookmakers=' || ? || '&fixtureId=' || l.provider_fixture_id"
            " WHERE l.rn = 1 AND l.start_ms <= ? AND l.start_ms >= ?"
            " AND coalesce(l.status_key, '') <> '3'"
            " AND (h.n IS NULL OR (h.n < ? AND h.last_ms <= ?))"
            " AND NOT EXISTS (SELECT 1 FROM raw_response r JOIN api_request a ON a.request_id = r.request_id"
            "   WHERE a.provider = ? AND a.endpoint = '/v4/historical-odds' AND r.usable = 1"
            "     AND a.params_canonical = 'bookmakers=' || ? || '&fixtureId=' || l.provider_fixture_id)"
            " ORDER BY l.start_ms LIMIT ?",
            (PROVIDER, PROVIDER, PROVIDER, self.cfg.op_bookmakers, cutoff, HISTORY_FROM_MS,
             self.cfg.op_history_404_max, recheck, PROVIDER, self.cfg.op_bookmakers,
             self.cfg.op_history_max_per_run if limit is None else limit)).fetchall()
        return [r[0] for r in rows]

    def history_one(self, slot: Slot, fid: str, basis: str) -> str:
        """Fetch one fixture's history. Returns 'usable', 'no_history' (a 404, recorded in
        request_outcome so the slot can still complete) or 'failed' (transport failure:
        429 after back-off, 5xx, network; fails the slot)."""
        res, req_id, status = self._call(slot, "/v4/historical-odds", self._history_params(fid), basis,
                                         attempts=3, terminal=(404,))
        if res is not None:
            return "usable"
        if status == 404:
            self.con.execute("INSERT INTO request_outcome VALUES (?, 'no_history', 404, ?)", (req_id, now_ms()))
            return "no_history"
        return "rate_limited" if status == 429 else "failed"

    def history(self, slot: Slot, ids: list[str] | None = None) -> dict:
        """Settled per fixture. Stops early after 2 consecutive fixtures that stayed
        rate-limited through back-off: the rest stay due for the next run."""
        ids = self.history_due(now_ms()) if ids is None else ids
        counts = {"usable": 0, "no_history": 0, "rate_limited": 0, "failed": 0}
        streak = 0
        done = 0
        for fid in ids:
            r = self.history_one(slot, fid, f"history {fid} (free)")
            counts[r] += 1
            done += 1
            streak = streak + 1 if r == "rate_limited" else 0
            if streak >= 2:
                break
        return {"requested": done, "due": len(ids), "stopped_early_on_429": streak >= 2, **counts}

    def history_probe(self, slot: Slot, tournaments: list[int], year: int, months: list[int],
                      control: str | None, t: int) -> dict:
        """One-off diagnostic of the real history window (free endpoint, 6 s pacing).
        Per month and tournament: the finished candidate fixture whose start is nearest
        after the 15th, at least 24 h ago. Months with no such fixture in the requested
        tournaments fall back to any configured tournament. Mapping is NOT required:
        this asks what the vendor has, not what we can use."""
        out = {"control": None, "months": {}}
        if control:
            out["control"] = {"fixture": control, "result": self.history_one(slot, control, f"probe control {control}")}
        all_t = [lg.oddspapi_tournament_id for lg in self.cfg.leagues if lg.oddspapi_tournament_id]
        for mth in months:
            lo = int(datetime(year, mth, 1, tzinfo=timezone.utc).timestamp() * 1000)
            hi = int(datetime(year + (mth == 12), mth % 12 + 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            mid = int(datetime(year, mth, 15, tzinfo=timezone.utc).timestamp() * 1000)
            rows = []
            for tid in tournaments:
                c = self._probe_pick(tid, lo, hi, mid, t)
                if c:
                    rows.append((tid, c))
            if not rows:
                for tid in all_t:
                    c = self._probe_pick(tid, lo, hi, mid, t)
                    if c:
                        rows.append((tid, c))
                        break
            res = []
            for tid, (fid, start, home, away) in rows:
                r = self.history_one(slot, fid, f"probe {year}-{mth:02d} t{tid} {fid}")
                st = self.con.execute(
                    "SELECT r.http_status FROM raw_response r JOIN api_request a ON a.request_id = r.request_id"
                    " WHERE a.slot_id = ? AND a.params_canonical = ? ORDER BY r.raw_id DESC LIMIT 1",
                    (slot.slot_id, f"bookmakers={self.cfg.op_bookmakers}&fixtureId={fid}")).fetchone()
                res.append({"tournament": tid, "fixture": fid, "match": f"{home} v {away}",
                            "start_utc": datetime.fromtimestamp(start / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                            "http_status": st[0] if st else None, "result": r})
            out["months"][f"{year}-{mth:02d}"] = res or "no finished candidate in any configured tournament"
        return out

    def _probe_pick(self, tid: int, lo: int, hi: int, mid: int, t: int):
        return self.con.execute(
            "WITH latest AS (SELECT provider_fixture_id, start_ms, status_key, home_team_name, away_team_name,"
            "  tournament_key, row_number() OVER (PARTITION BY provider_fixture_id ORDER BY raw_id DESC) AS rn"
            "  FROM provider_fixture_candidate WHERE provider = ?)"
            " SELECT provider_fixture_id, start_ms, home_team_name, away_team_name FROM latest"
            " WHERE rn = 1 AND tournament_key = ? AND status_key = '2' AND start_ms >= ? AND start_ms < ?"
            " AND start_ms <= ? ORDER BY (start_ms < ?), abs(start_ms - ?) LIMIT 1",
            (PROVIDER, str(tid), lo, hi, t - 86_400_000, mid, mid)).fetchone()
