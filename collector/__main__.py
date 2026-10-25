"""Command-line entry point.  Usage:  python -m collector --config config.toml <command> [args]

Scheduled (systemd timers):  plan, watchdog, af-fixtures, af-odds, af-results,
                             op-account, op-fixtures, op-history, report
Day 1 (by hand):             init, af-bootstrap, op-account, op-catalog,
                             op-fixtures --force, op-check-fixture
Mapping (by hand):           map-propose, map-load-teams, map-apply, map-audit, map-sample
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import diag, ledger, mapping
from .af import AF
from .config import load
from .http import Fetcher
from .op import OP, QuotaRefused
from .store import Archive, now_ms, open_db

JOB_OF = {"af-fixtures": "af_fixtures", "af-odds": "af_odds", "af-results": "af_results",
          "op-account": "op_account", "op-fixtures": "op_fixtures", "op-history": "op_history",
          "af-bootstrap": "af_bootstrap", "op-catalog": "op_catalog", "op-check-fixture": "op_history",
          "op-backfill": "op_backfill", "af-backfill": "af_results"}


def ping(cfg, cmd: str, suffix: str = "") -> None:
    url = cfg.healthchecks.get(cmd.replace("-", "_"))
    if not url:
        return
    try:
        urllib.request.urlopen(url.rstrip("/") + suffix, timeout=10).read()
    except Exception:
        pass   # monitoring must never break collection


def out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="collector")
    ap.add_argument("--config", default="config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("init", "plan", "watchdog", "af-fixtures", "af-odds", "af-results", "af-bootstrap", "op-account",
              "op-history", "report", "map-propose", "map-apply", "map-audit", "map-sample"):
        sub.add_parser(c)
    p = sub.add_parser("op-fixtures")
    p.add_argument("--force", action="store_true", help="list every configured tournament now (billable)")
    p = sub.add_parser("op-catalog")
    p.add_argument("--sport-id", type=int, required=True)
    p = sub.add_parser("op-check-fixture")
    p.add_argument("fixture_id")
    p.add_argument("--kickoff", help="ISO kickoff time, e.g. 2026-10-10T16:30:00Z (else from candidates)")
    p = sub.add_parser("af-backfill", help="outcomes for finished fixtures older than 72 h (exploration seasons)")
    p.add_argument("--max", type=int, default=1500)
    p = sub.add_parser("op-backfill")
    p.add_argument("--max", type=int, default=600)
    p = sub.add_parser("map-load-teams")
    p.add_argument("csv")
    p.add_argument("commit")
    a = ap.parse_args(argv)

    cfg = load(a.config)
    cfg.review_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.db_path.parent / f".lock-{a.cmd}"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"{a.cmd}: previous run still active; exiting", file=sys.stderr)
        return 3

    con = open_db(cfg.db_path)
    if a.cmd == "init":
        out({"db": str(cfg.db_path), "tables": con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]})
        return 0
    if a.cmd == "plan":
        out({"slots_planned": ledger.plan(con)})
        return 0
    if a.cmd == "watchdog":
        missed = ledger.watchdog(con)
        out({"missed": missed})
        if missed:
            ping(cfg, "watchdog", "/fail")
            return 1
        ping(cfg, "watchdog")
        return 0
    if a.cmd == "report":
        out(report(con))
        return 0
    if a.cmd.startswith("map-"):
        return mapping_cmd(cfg, con, a)

    fetcher = Fetcher(con, Archive(cfg.archive_dir))
    job = JOB_OF[a.cmd]
    scheduled = a.cmd in ("af-fixtures", "af-odds", "af-results", "op-account", "op-fixtures", "op-history")
    ping(cfg, a.cmd, "/start")
    try:
        with ledger.run_slot(con, job, scheduled_for=None if scheduled else now_ms(), unplanned_ok=not scheduled) as slot:
            result = dispatch(cfg, con, fetcher, slot, a)
        out(result)
        ping(cfg, a.cmd)
        return 0
    except QuotaRefused as e:
        print(f"QUOTA REFUSED: {e}", file=sys.stderr)
        ping(cfg, a.cmd, "/fail")
        return 2
    except Exception as e:
        print(f"{a.cmd} FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        ping(cfg, a.cmd, "/fail")
        return 1


def dispatch(cfg, con, fetcher, slot, a):
    if a.cmd == "af-bootstrap":
        r = AF(cfg, con, fetcher).bootstrap(slot)
        r["pinnacle_check"] = diag.bookmaker_check(r["bookmakers"])
        (cfg.review_dir / "day1_api_football.json").write_text(json.dumps(r, indent=2, default=str))
        return {k: r[k] for k in ("pinnacle_check", "pinnacle_exact_name_matches", "pinnacle_like_names", "bet_count")}
    if a.cmd == "af-fixtures":
        return {"state_changes": AF(cfg, con, fetcher).fixtures(slot)}
    if a.cmd == "af-odds":
        return {"league_dates": AF(cfg, con, fetcher).odds(slot)}
    if a.cmd == "af-results":
        return AF(cfg, con, fetcher).results(slot)
    if a.cmd == "af-backfill":
        t = now_ms()
        return AF(cfg, con, fetcher).results(slot, since_ms=t - 400 * 86_400_000, until_ms=t - 72 * 3_600_000,
                                             limit=a.max)
    op = OP(cfg, con, fetcher)
    if a.cmd == "op-account":
        acct = op.account(slot)
        acct["our_billable_calls_this_month"] = op.our_billable_this_month(now_ms())
        return acct
    if a.cmd == "op-catalog":
        r = op.catalog(slot, a.sport_id)
        (cfg.review_dir / "day1_oddspapi_tournaments.json").write_text(json.dumps(r["tournaments"], indent=2))
        return {"tournaments_saved_to": str(cfg.review_dir / "day1_oddspapi_tournaments.json"),
                "markets_raw_id": r["markets_raw_id"]}
    if a.cmd == "op-fixtures":
        force = [l.oddspapi_tournament_id for l in cfg.leagues if l.oddspapi_tournament_id] if a.force else None
        return op.fixtures(slot, force=force)
    if a.cmd == "op-history":
        return op.history(slot)
    if a.cmd == "op-backfill":
        ids = op.history_due(now_ms())
        ids = ids[: a.max]
        return op.history(slot, ids=ids)
    if a.cmd == "op-check-fixture":
        res = op.history(slot, ids=[a.fixture_id])
        if not res["usable"]:
            return {"fixture": a.fixture_id, "error": "history call did not return a usable response"}
        body = json.loads(json.dumps(latest_body(con, a.fixture_id, cfg.op_bookmakers)))
        if a.kickoff:
            ko = diag.iso_ms(a.kickoff)
            ko_src = "--kickoff argument"
        else:
            row = con.execute("SELECT start_ms FROM provider_fixture_candidate WHERE provider='oddspapi'"
                              " AND provider_fixture_id=? ORDER BY raw_id DESC LIMIT 1", (a.fixture_id,)).fetchone()
            st = (body or {}).get("startTime") or (body or {}).get("trueStartTime")
            ko = row[0] if row else (diag.iso_ms(st) if st else None)
            ko_src = "OddsPapi candidate" if row else ("payload startTime" if st else None)
        if ko is None:
            return {"fixture": a.fixture_id, "error": "kickoff unknown: rerun with --kickoff"}
        rep = diag.close_window_report(body, ko, book=cfg.op_bookmakers.split(",")[0])
        rep["kickoff_source"] = ko_src
        (cfg.review_dir / f"check_{a.fixture_id}.json").write_text(json.dumps(rep, indent=2))
        return rep
    raise ValueError(a.cmd)


def latest_body(con, fixture_id: str, books: str):
    from .store import Archive as A
    row = con.execute(
        "SELECT r.archive_path FROM raw_response r JOIN api_request a ON a.request_id=r.request_id"
        " WHERE a.provider='oddspapi' AND a.endpoint='/v4/historical-odds' AND r.usable=1"
        " AND a.params_canonical = ? ORDER BY r.raw_id DESC LIMIT 1",
        (f"bookmakers={books}&fixtureId={fixture_id}",)).fetchone()
    return json.loads(A.read(Path(row[0]))["body"])


def mapping_cmd(cfg, con, a) -> int:
    if a.cmd == "map-propose":
        path = cfg.review_dir / "team_map_proposals.csv"
        out({"proposed_rows": mapping.propose_teams(cfg, con, path), "csv": str(path),
             "next": "review the CSV, delete wrong rows, commit it, then run map-load-teams <csv> <commit>"})
    elif a.cmd == "map-load-teams":
        out({"loaded": mapping.load_teams(con, Path(a.csv), a.commit)})
    elif a.cmd == "map-apply":
        out(mapping.apply(cfg, con))
    elif a.cmd == "map-audit":
        out({"kickoff_recheck": mapping.audit_kickoffs(con),
             "misjoin_rate": con.execute("SELECT * FROM v_misjoin_rate").fetchall()})
    elif a.cmd == "map-sample":
        path = cfg.review_dir / "manual_mapping_sample.csv"
        out({"sampled": mapping.manual_sample(con, path), "csv": str(path)})
    return 0


def report(con) -> dict:
    t = now_ms()
    day = t - 86_400_000
    q = lambda sql, *p: con.execute(sql, p).fetchall()  # noqa: E731
    quota = q("SELECT request_limit, request_count, observed_at_ms FROM quota_obs ORDER BY observed_at_ms DESC LIMIT 1")
    return {
        "generated_utc": datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat(),
        "slots_last_24h": q("SELECT job, status, count(*) FROM capture_slot WHERE scheduled_for_ms BETWEEN ? AND ?"
                            " GROUP BY job, status ORDER BY job, status", day, t),
        "raw_last_24h": q("SELECT a.provider, a.endpoint, sum(r.usable), count(*) FROM raw_response r"
                          " JOIN api_request a ON a.request_id=r.request_id WHERE r.received_at_ms > ?"
                          " GROUP BY a.provider, a.endpoint", day),
        "clock_flagged_last_24h": q("SELECT count(*) FROM raw_response WHERE received_at_ms > ? AND http_status=200"
                                    " AND clock_ok=0", day)[0][0],
        "oddspapi_quota_latest": quota[0] if quota else None,
        "fixtures_tracked": q("SELECT count(*) FROM fixture")[0][0],
        "outcomes": q("SELECT count(*) FROM outcome_obs")[0][0],
        "oddspapi_candidates": q("SELECT count(DISTINCT provider_fixture_id) FROM provider_fixture_candidate")[0][0],
        "oddspapi_history_usable": q("SELECT count(DISTINCT a.params_canonical) FROM raw_response r JOIN api_request a"
                                     " ON a.request_id=r.request_id WHERE a.endpoint='/v4/historical-odds' AND r.usable=1")[0][0],
        "mappings_live": q("SELECT count(*) FROM provider_fixture_map WHERE revoked=0")[0][0],
        "misjoin_rate": q("SELECT * FROM v_misjoin_rate"),
    }


if __name__ == "__main__":
    sys.exit(main())
