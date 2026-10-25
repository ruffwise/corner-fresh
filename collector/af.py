"""api-football jobs: bootstrap, fixtures, odds, results.

Parsing done here is limited to what scheduling needs (leagues, teams,
fixtures, kickoff/status history, outcomes). Odds are archived raw only;
price parsing waits for the reviewed allowlists (build step M6).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .config import Config
from .http import Fetcher, Result
from .ledger import Slot
from .store import now_ms

PROVIDER = "api_football"
FINISHED = ("FT", "AET", "PEN")
VOIDISH = ("PST", "CANC", "ABD", "AWD", "WO")


def af_errors(body: object) -> str:
    """api-football reports errors as [] when fine, or a list/dict of messages."""
    errs = body.get("errors") if isinstance(body, dict) else None
    return "[]" if not errs else json.dumps(errs, sort_keys=True)


class AF:
    def __init__(self, cfg: Config, con: sqlite3.Connection, fetcher: Fetcher):
        if not cfg.af_key:
            raise RuntimeError("AF_KEY is not set")
        self.cfg, self.con, self.f = cfg, con, fetcher

    # --- transport -------------------------------------------------------------------
    def _attempt(self, request_id: int, endpoint: str, params: dict, page: int, attempt: int) -> Result:
        self.f.pace(PROVIDER, self.cfg.af_min_interval_s)
        res = self.f.get(request_id=request_id, provider=PROVIDER, paged=True, url=self.cfg.af_base + endpoint,
                         params=params, secret_headers={"x-apisports-key": self.cfg.af_key},
                         page=page, attempt=attempt, errors_of=af_errors)
        remaining = res.headers.get("x-ratelimit-remaining")
        if remaining is not None and remaining.isdigit() and int(remaining) < 3:
            self.f.sleep(61)   # per-minute cap nearly hit: wait out the window rather than burst
        return res

    def call(self, slot: Slot, endpoint: str, params: dict, basis: str, max_attempts: int = 3) -> list[Result]:
        """Fetch every page of one logical request. Marks the api_request complete only
        when the schema trigger accepts it (all pages present and usable)."""
        slot.expect(1, basis)
        req_id = self.f.request_row(slot.slot_id, PROVIDER, endpoint, params)
        pages: dict[int, Result] = {}
        total = None
        page = 1
        while True:
            q = dict(params)
            if page > 1:
                q["page"] = page
            res = None
            for attempt in range(1, max_attempts + 1):
                res = self._attempt(req_id, endpoint, q, page, attempt)
                if res.usable:
                    break
                self.f.sleep(min(30, 2 ** attempt))
            if not res.usable:
                break
            pages[page] = res
            pg = res.body.get("paging") or {}
            total = pg.get("total") or 1
            if page >= total:
                break
            page += 1
        if total is not None and len(pages) == total:
            self.con.execute("UPDATE api_request SET expected_pages=? WHERE request_id=?", (total, req_id))
            self.con.execute("UPDATE api_request SET status='complete' WHERE request_id=?", (req_id,))
        else:
            self.con.execute("UPDATE api_request SET status='failed' WHERE request_id=?", (req_id,))
        return [pages[p] for p in sorted(pages)]

    # --- bootstrap (day 1) -------------------------------------------------------------
    def bootstrap(self, slot: Slot) -> dict:
        report: dict = {}
        st = self.call(slot, "/status", {}, "account status")
        report["status"] = st[0].body.get("response") if st else None
        books = self.call(slot, "/odds/bookmakers", {}, "bookmaker list")
        names = sorted({(b["id"], b["name"]) for r in books for b in r.body.get("response", [])})
        report["bookmakers"] = names
        report["pinnacle_exact_name_matches"] = [n for n in names if n[1] == "Pinnacle"]
        report["pinnacle_like_names"] = [n for n in names if "pinnacle" in n[1].lower()]
        bets = self.call(slot, "/odds/bets", {}, "pre-match bet list")
        report["bet_count"] = sum(len(r.body.get("response", [])) for r in bets)
        for lg in self.cfg.leagues:
            self.ensure_league(slot, lg.league_id, lg.season)
        return report

    def ensure_league(self, slot: Slot, league_id: int, season: int) -> None:
        if self.con.execute("SELECT 1 FROM league WHERE league_id=? AND season=?", (league_id, season)).fetchone():
            return
        res = self.call(slot, "/leagues", {"id": league_id, "season": season}, f"league {league_id}/{season}")
        items = res[0].body.get("response", []) if res else []
        if len(items) != 1:
            raise RuntimeError(f"/leagues id={league_id} season={season} returned {len(items)} items")
        it = items[0]
        self.con.execute("INSERT INTO league VALUES (?,?,?,?,?)",
                         (league_id, season, it["league"]["name"], it["country"]["name"], it["league"]["type"]))

    # --- fixtures --------------------------------------------------------------------------
    @staticmethod
    def et_rule(league_type: str, round_name: str) -> tuple[int, str]:
        """Pre-match, conservative: extra time is impossible only in league formats."""
        if league_type == "League":
            return 0, "league format: no extra time"
        r = round_name.lower()
        if any(k in r for k in ("league stage", "group", "regular season")):
            return 0, f"cup round '{round_name}': league/group stage, no extra time"
        return 1, f"cup round '{round_name}': knockout or unknown, extra time possible"

    def record_fixtures(self, res: Result) -> int:
        """Insert new fixtures; append a state observation only when kickoff or status changed."""
        n = 0
        for it in res.body.get("response", []):
            fx, lg, tm = it["fixture"], it["league"], it["teams"]
            fid = fx["id"]
            for side in ("home", "away"):
                self.con.execute("INSERT OR IGNORE INTO team VALUES (?, ?)", (tm[side]["id"], tm[side]["name"]))
            row = self.con.execute("SELECT league_type FROM league WHERE league_id=? AND season=?",
                                   (lg["id"], lg["season"])).fetchone()
            if row is None:
                continue   # league not tracked
            if not self.con.execute("SELECT 1 FROM fixture WHERE fixture_id=?", (fid,)).fetchone():
                et, basis = self.et_rule(row[0], lg.get("round") or "")
                self.con.execute("INSERT INTO fixture VALUES (?,?,?,?,?,?,?,?,?)",
                                 (fid, lg["id"], lg["season"], lg.get("round") or "", tm["home"]["id"],
                                  tm["away"]["id"], res.raw_id, et, basis))
            ko = int(fx["timestamp"]) * 1000
            status = fx["status"]["short"]
            last = self.con.execute("SELECT kickoff_ms, status_short FROM v_fixture_latest WHERE fixture_id=?",
                                    (fid,)).fetchone()
            if last != (ko, status):
                self.con.execute("INSERT INTO fixture_state_obs (fixture_id, raw_id, observed_at_ms, kickoff_ms,"
                                 " status_short) VALUES (?,?,?,?,?)", (fid, res.raw_id, now_ms(), ko, status))
                n += 1
        return n

    def fixtures(self, slot: Slot) -> int:
        changed = 0
        for lg in self.cfg.leagues:
            self.ensure_league(slot, lg.league_id, lg.season)
            for res in self.call(slot, "/fixtures", {"league": lg.league_id, "season": lg.season},
                                 f"season fixtures {lg.league_id}/{lg.season}"):
                changed += self.record_fixtures(res)
        return changed

    # --- odds snapshots ---------------------------------------------------------------------
    def odds_targets(self, t: int) -> list[tuple[int, int, str]]:
        """(league, season, UTC date) for every date with a not-started fixture in the horizon."""
        horizon = t + self.cfg.af_odds_horizon_days * 86_400_000
        rows = self.con.execute(
            "SELECT DISTINCT f.league_id, f.season, l.kickoff_ms FROM fixture f JOIN v_fixture_latest l USING (fixture_id)"
            " WHERE l.status_short IN ('NS','TBD') AND l.kickoff_ms > ? AND l.kickoff_ms <= ?", (t, horizon)).fetchall()
        tracked = {(x.league_id, x.season) for x in self.cfg.leagues}
        out = {(lid, s, datetime.fromtimestamp(ko / 1000, tz=timezone.utc).strftime("%Y-%m-%d"))
               for lid, s, ko in rows if (lid, s) in tracked}
        return sorted(out)

    def odds(self, slot: Slot) -> int:
        targets = self.odds_targets(now_ms())
        for lid, season, day in targets:
            self.call(slot, "/odds", {"league": lid, "season": season, "date": day},
                      f"odds {lid}/{season} {day}")
        return len(targets)

    # --- results and outcomes --------------------------------------------------------------------
    def results(self, slot: Slot, since_ms: int | None = None, until_ms: int | None = None,
                limit: int = 10_000) -> dict:
        """Default window: kickoffs from 72 h ago to 6 h ahead (the hourly job).
        The backfill passes an older window to collect outcomes for exploration seasons."""
        t = now_ms()
        since_ms = t - 72 * 3_600_000 if since_ms is None else since_ms
        until_ms = t + 6 * 3_600_000 if until_ms is None else until_ms
        due = [r[0] for r in self.con.execute(
            "SELECT l.fixture_id FROM v_fixture_latest l WHERE l.kickoff_ms BETWEEN ? AND ?"
            " AND NOT EXISTS (SELECT 1 FROM outcome_obs o WHERE o.fixture_id = l.fixture_id"
            "                  AND o.source='api_football' AND o.status_short IN ('FT','AET','PEN'))"
            " AND l.status_short NOT IN ('PST','CANC','ABD','AWD','WO')"
            " ORDER BY l.kickoff_ms LIMIT ?", (since_ms, until_ms, limit)).fetchall()]
        finished = []
        for i in range(0, len(due), 20):
            chunk = due[i:i + 20]
            for res in self.call(slot, "/fixtures", {"ids": "-".join(map(str, chunk))},
                                 f"fixture state for {len(chunk)} ids"):
                self.record_fixtures(res)
                for it in res.body.get("response", []):
                    if it["fixture"]["status"]["short"] in FINISHED:
                        finished.append((it, res.raw_id))
        recorded = waiting = 0
        for it, raw_id in finished:
            fid = it["fixture"]["id"]
            ft = (it.get("score") or {}).get("fulltime") or {}   # regular-time score (assumption A3)
            ch, ca, c_raw = self.corners(slot, fid, it["teams"]["home"]["id"], it["teams"]["away"]["id"])
            kickoff = int(it["fixture"]["timestamp"]) * 1000
            if ch is None and kickoff > t - 24 * 3_600_000:
                waiting += 1           # statistics not published yet: retry next run, up to 24 h
                continue
            self.con.execute(
                "INSERT INTO outcome_obs (fixture_id, source, raw_id, recorded_at_ms, status_short, goals_home_90,"
                " goals_away_90, corners_home, corners_away, corners_raw_id)"
                " VALUES (?, 'api_football', ?, ?, ?, ?, ?, ?, ?, ?)",
                (fid, raw_id, now_ms(), it["fixture"]["status"]["short"], ft.get("home"), ft.get("away"),
                 ch, ca, c_raw if ch is not None else None))
            recorded += 1
        return {"checked": len(due), "outcomes_recorded": recorded, "awaiting_statistics": waiting}

    def corners(self, slot: Slot, fid: int, home_id: int, away_id: int) -> tuple[int | None, int | None, int | None]:
        res = self.call(slot, "/fixtures/statistics", {"fixture": fid}, f"statistics {fid}")
        if not res:
            return None, None, None
        vals = {}
        for team_block in res[0].body.get("response", []):
            for s in team_block.get("statistics", []):
                if s.get("type") == "Corner Kicks":          # exact label only
                    vals[team_block["team"]["id"]] = s.get("value")
        h, a = vals.get(home_id), vals.get(away_id)
        if not (isinstance(h, int) and isinstance(a, int)):
            return None, None, res[0].raw_id
        return h, a, res[0].raw_id
