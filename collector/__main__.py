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

from . import close, diag, ledger, mapping
from .af import AF
from .config import load
from .http import Fetcher
from .op import OP, QuotaRefused
from .store import Archive, init_db, now_ms, open_db, seed

JOB_OF = {"af-fixtures": "af_fixtures", "af-odds": "af_odds", "af-results": "af_results",
          "op-account": "op_account", "op-fixtures": "op_fixtures", "op-history": "op_history",
          "af-bootstrap": "af_bootstrap", "op-catalog": "op_catalog", "op-check-fixture": "op_history",
          "op-backfill": "op_backfill", "af-backfill": "af_results", "repair-damaged": "af_results"}


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
    p = sub.add_parser("archive-scrub", help="redact personal data from already-archived PERSONAL-endpoint bodies")
    p.add_argument("--apply", action="store_true", help="rewrite (default: dry run)")
    p.add_argument("--expect", type=int, help="required with --apply: the rewrite count the dry run printed")
    p = sub.add_parser("archive-finalize", help="delete superseded originals after you have checked the rewrites")
    p.add_argument("--raw-ids", required=True, help="comma-separated raw ids")
    p.add_argument("--confirmed-by", required=True)
    sub.add_parser("archive-verify")
    p = sub.add_parser("repair-damaged", help="re-fetch responses whose data was altered by redact-v1 (dry run by default)")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--expect", type=int, help="required with --apply: the damaged-row count the dry run printed")
    p = sub.add_parser("export-fixtures", help="copy redacted real payloads into tests/fixtures/real for the test suite")
    p.add_argument("--out", required=True)
    p.add_argument("--history", action="append", default=[], metavar="FIXTUREID=ISO_KICKOFF")
    for c in ("init", "migrate", "check-config", "plan", "watchdog", "af-fixtures", "af-odds", "af-results", "af-bootstrap", "op-account",
              "op-history", "report", "map-propose", "map-apply", "map-audit", "map-sample"):
        sub.add_parser(c)
    p = sub.add_parser("op-fixtures")
    p.add_argument("--force", action="store_true", help="list every configured tournament now (billable)")
    p = sub.add_parser("op-catalog")
    p.add_argument("--sport-id", type=int, required=True)
    p = sub.add_parser("op-check-fixture")
    p.add_argument("fixture_id")
    p.add_argument("--kickoff", help="scheduled kickoff, ISO UTC, e.g. 2026-10-10T11:30:00Z")
    p.add_argument("--af-fixture-id", type=int, help="take kickoff, status and first-half start from api-football data")
    p.add_argument("--status", default="FT", help="api-football status when --kickoff is used (default FT)")
    p.add_argument("--af-first-half-start", help="ISO UTC actual start, if known")
    p = sub.add_parser("af-backfill", help="outcomes for finished fixtures older than 72 h (exploration seasons)")
    p.add_argument("--max", type=int, default=1500)
    p = sub.add_parser("op-backfill")
    p.add_argument("--max", type=int, default=600)
    p = sub.add_parser("map-load-teams", help="load a reviewed team CSV exactly as committed in git")
    p.add_argument("commit", help="full 40-character commit hash")
    p.add_argument("path_in_repo", help="e.g. review/team_map_v1.csv")
    a = ap.parse_args(argv)

    cfg = load(a.config)
    if a.cmd == "check-config":          # read-only: creates nothing, not even a lock file
        return check_config(cfg)
    cfg.review_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.db_path.parent / f".lock-{a.cmd}"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"{a.cmd}: previous run still active; exiting", file=sys.stderr)
        return 3

    if a.cmd == "init":
        con = init_db(cfg.db_path)
        out({"db": str(cfg.db_path), "tables": con.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]})
        return 0
    if a.cmd == "migrate":
        return migrate_cmd(cfg)
    con = open_db(cfg.db_path)
    if a.cmd == "plan":
        out({"slots_planned": ledger.plan(con)})
        return 0
    if a.cmd == "watchdog":
        missed = ledger.watchdog(con)
        missed += [(f"redaction_on_data_endpoint:{r[1]} {r[2]}:{r[3]}", r[0]) for r in con.execute(
            "SELECT raw_id, provider, endpoint, rule FROM v_redaction_alerts WHERE received_at_ms > ?",
            (now_ms() - 86_400_000,))]
        out({"missed": missed})
        if missed:
            ping(cfg, "watchdog", "/fail")
            return 1
        ping(cfg, "watchdog")
        return 0
    if a.cmd == "report":
        out(report(con, cfg))
        return 0
    if a.cmd.startswith("map-"):
        return mapping_cmd(cfg, con, a)
    if a.cmd == "archive-scrub":
        from .archive_tools import scrub
        out(scrub(con, Archive(cfg.archive_dir), (cfg.af_key, cfg.op_key), apply=a.apply, expect=a.expect,
                  superseded_dir=cfg.db_path.parent / "superseded"))
        return 0
    if a.cmd == "archive-finalize":
        from .archive_tools import finalize
        out(finalize(con, [int(x) for x in a.raw_ids.split(",") if x.strip()], a.confirmed_by))
        return 0
    if a.cmd == "repair-damaged" and not a.apply:
        from .archive_tools import damaged
        rows = damaged(con)
        by_ep = {}
        for d in rows:
            k = f"{d['provider']} {d['endpoint']}"
            by_ep.setdefault(k, {"rows": 0, "changed_values": 0, "patterns": {}})
            by_ep[k]["rows"] += 1
            by_ep[k]["changed_values"] += d["changed_values"]
            for pat, n in d["patterns"].items():
                by_ep[k]["patterns"][pat] = by_ep[k]["patterns"].get(pat, 0) + n
        out({"dry_run": True, "damaged_rows": len(rows), "per_endpoint": by_ep,
             "raw_ids": [d["raw_id"] for d in rows],
             "next": f"if this is what you expect, rerun with --apply --expect {len(rows)}"})
        return 0
    if a.cmd == "export-fixtures":
        from .archive_tools import export_fixtures
        ko = dict(h.split("=", 1) for h in a.history)
        r = export_fixtures(con, Path(a.out), (cfg.af_key, cfg.op_key), ko)
        out(r)
        return 0
    if a.cmd == "archive-verify":
        from .archive_tools import verify
        r = verify(con, (cfg.af_key, cfg.op_key))
        out(r)
        return 0 if r["problem_count"] == 0 else 1

    fetcher = Fetcher(con, Archive(cfg.archive_dir), secrets=(cfg.af_key, cfg.op_key))
    job = JOB_OF[a.cmd]
    scheduled = a.cmd in ("af-fixtures", "af-odds", "af-results", "op-account", "op-fixtures", "op-history")
    ping(cfg, a.cmd, "/start")
    try:
        with ledger.run_slot(con, job, scheduled_for=None if scheduled else now_ms(), unplanned_ok=not scheduled) as slot:
            result = dispatch(cfg, con, fetcher, slot, a)
        out(result)
        if isinstance(result, dict) and result.get("alert"):
            print(f"{a.cmd} ALERT: unmapped share above threshold for tournaments {result['alert']}", file=sys.stderr)
            ping(cfg, a.cmd, "/fail")
            return 4
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
        return {k: r[k] for k in ("pinnacle_check", "pinnacle_exact_name_matches", "pinnacle_like_names",
                                  "unnamed_bookmaker_ids", "bet_count")}
    if a.cmd == "af-fixtures":
        return {"state_changes": AF(cfg, con, fetcher).fixtures(slot)}
    if a.cmd == "af-odds":
        return {"league_dates": AF(cfg, con, fetcher).odds(slot)}
    if a.cmd == "af-results":
        return AF(cfg, con, fetcher).results(slot)
    if a.cmd == "repair-damaged":
        from .archive_tools import damaged, refetch
        rows = damaged(con)
        if a.expect is None or a.expect != len(rows):
            raise RuntimeError(f"--expect {a.expect} does not match {len(rows)} damaged rows; nothing fetched")
        return refetch(con, AF(cfg, con, fetcher), slot, rows)
    if a.cmd == "af-backfill":
        t = now_ms()
        return AF(cfg, con, fetcher).results(slot, since_ms=t - 400 * 86_400_000, until_ms=t - 72 * 3_600_000,
                                             limit=a.max)
    op = OP(cfg, con, fetcher)
    if a.cmd == "op-account":
        acct = op.account(slot)
        body = acct.pop("raw")
        # Never print the body: only the field names it carried (values may be personal).
        acct["fields_present"] = sorted(body.keys()) if isinstance(body, dict) else type(body).__name__
        acct["our_billable_calls_this_month"] = op.our_billable_this_month(now_ms())
        return acct
    if a.cmd == "op-catalog":
        r = op.catalog(slot, a.sport_id)
        (cfg.review_dir / "day1_oddspapi_tournaments.json").write_text(json.dumps(r["tournaments"], indent=2))
        return {"tournaments_saved_to": str(cfg.review_dir / "day1_oddspapi_tournaments.json"),
                "markets_raw_id": r["markets_raw_id"]}
    if a.cmd == "op-fixtures":
        tids = sorted({l.oddspapi_tournament_id for l in cfg.leagues if l.oddspapi_tournament_id})
        if a.force:
            todo = tids
        else:
            t = now_ms()
            mapping.apply(cfg, con)
            um = mapping.unmapped_upcoming(cfg, con)
            relist = [int(k) for k, r in um.items() if r["needs_relisting"]
                      and (op.last_listing_ms(int(k)) or 0) < t - 48 * 3_600_000]
            todo = sorted(set(op.fixtures_due(t)) | set(relist))
        res = op.fixtures(slot, force=todo)
        res["mapping"] = mapping.apply(cfg, con)
        res["unmapped_next_72h"] = mapping.unmapped_upcoming(cfg, con)
        res["alert"] = sorted(k for k, r in res["unmapped_next_72h"].items() if r["alert"])
        return res
    if a.cmd == "op-history":
        res = {"mapping": mapping.apply(cfg, con)}
        res.update(op.history(slot))
        return res
    if a.cmd == "op-backfill":
        ids = op.history_due(now_ms())
        ids = ids[: a.max]
        return op.history(slot, ids=ids)
    if a.cmd == "op-check-fixture":
        res = op.history(slot, ids=[a.fixture_id])
        if not res["usable"]:
            return {"fixture": a.fixture_id, "error": "history call did not return a usable response"}
        body = latest_body(con, a.fixture_id, cfg.op_bookmakers)
        sched, status, af_first = kickoff_inputs(con, a)
        if sched is None:
            return {"fixture": a.fixture_id, "error": "scheduled kickoff unknown: pass --kickoff or --af-fixture-id"}
        row = con.execute("SELECT true_start_ms FROM provider_fixture_candidate WHERE provider='oddspapi'"
                          " AND provider_fixture_id=? AND true_start_ms IS NOT NULL ORDER BY raw_id DESC LIMIT 1",
                          (a.fixture_id,)).fetchone()
        op_true = close.payload_true_start(body) or (row[0] if row else None)
        cut = close.cutoff(sched, status, af_first, op_true)
        rep = close.fixture_close_report(body, cut, book=cfg.op_bookmakers.split(",")[0])
        rep["fixture"] = a.fixture_id
        rep["payload_top_level_fields"] = sorted(body.keys()) if isinstance(body, dict) else None
        (cfg.review_dir / f"check_{a.fixture_id}.json").write_text(json.dumps(rep, indent=2))
        return rep
    raise ValueError(a.cmd)


def kickoff_inputs(con, a):
    """(scheduled kickoff, status, api-football first-half start) for op-check-fixture."""
    if a.af_fixture_id:
        row = con.execute("SELECT kickoff_ms, status_short FROM v_fixture_latest WHERE fixture_id=?",
                          (a.af_fixture_id,)).fetchone()
        if row is None:
            return None, None, None
        first = con.execute("SELECT first_half_start_ms FROM outcome_obs WHERE fixture_id=? AND source='api_football'"
                            " ORDER BY recorded_at_ms DESC LIMIT 1", (a.af_fixture_id,)).fetchone()
        return row[0], row[1], first[0] if first else None
    if not a.kickoff:
        return None, None, None
    first = close.iso_ms(a.af_first_half_start) if a.af_first_half_start else None
    return close.iso_ms(a.kickoff), a.status, first


def latest_body(con, fixture_id: str, books: str):
    from .store import Archive as A
    row = con.execute(
        "SELECT l.archive_path FROM raw_response r JOIN api_request a ON a.request_id=r.request_id"
        " JOIN v_archive_location l ON l.raw_id = r.raw_id"
        " WHERE a.provider='oddspapi' AND a.endpoint='/v4/historical-odds' AND r.usable=1"
        " AND a.params_canonical = ? ORDER BY r.raw_id DESC LIMIT 1",
        (f"bookmakers={books}&fixtureId={fixture_id}",)).fetchone()
    return json.loads(A.read(Path(row[0]))["body"])


def migrate_cmd(cfg) -> int:
    """Back up (with restore test), then apply pending migrations. Safe to re-run."""
    import sys as _s
    from .backup import backup
    _s.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
    from connect import SCHEMA_VERSION, apply_migrations, assert_schema_contract, connect
    con = connect(cfg.db_path, check=False)
    before = con.execute("PRAGMA user_version").fetchone()[0]
    if before == SCHEMA_VERSION:
        assert_schema_contract(con)
        out({"migrated": [], "user_version": before, "note": "already current"})
        return 0
    bk = backup(cfg.db_path, cfg.db_path.parent / "backups", label=f"pre-migrate-v{before}")
    applied = apply_migrations(con)
    assert_schema_contract(con)
    seed(con)
    out({"backup": bk["backup"], "backup_restore_test": bk["restore_test"], "migrated": applied,
         "user_version": con.execute("PRAGMA user_version").fetchone()[0]})
    return 0


def check_config(cfg) -> int:
    """Print what this config would touch, without creating anything."""
    exists = cfg.db_path.exists()
    info = {"db_path": str(cfg.db_path), "db_exists": exists, "archive_dir": str(cfg.archive_dir),
            "archive_exists": cfg.archive_dir.exists(), "review_dir": str(cfg.review_dir),
            "leagues": len(cfg.leagues), "af_key_set": bool(cfg.af_key), "op_key_set": bool(cfg.op_key),
            "healthchecks": sorted(cfg.healthchecks)}
    if exists:
        import sqlite3 as _sq
        c = _sq.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
        info["db_user_version"] = c.execute("PRAGMA user_version").fetchone()[0]
        info["fixtures"] = c.execute("SELECT count(*) FROM fixture").fetchone()[0]
    out(info)
    return 0 if exists else 1


def mapping_cmd(cfg, con, a) -> int:
    if a.cmd == "map-propose":
        path = cfg.review_dir / "team_map_proposals.csv"
        out({"proposed_rows": mapping.propose_teams(cfg, con, path), "csv": str(path),
             "next": "review the CSV, delete wrong rows, commit it under review/, then run "
                     "map-load-teams <commit> review/<file>.csv"})
    elif a.cmd == "map-load-teams":
        out({"loaded": mapping.load_teams_from_git(con, cfg.repo_dir, a.commit, a.path_in_repo),
             "from": f"{a.commit}:{a.path_in_repo}"})
    elif a.cmd == "map-apply":
        out(mapping.apply(cfg, con))
    elif a.cmd == "map-audit":
        out({"kickoff_recheck": mapping.audit_kickoffs(con),
             "misjoin_rate": con.execute("SELECT * FROM v_misjoin_rate").fetchall()})
    elif a.cmd == "map-sample":
        path = cfg.review_dir / "manual_mapping_sample.csv"
        out({"sampled": mapping.manual_sample(con, path), "csv": str(path)})
    return 0


def report(con, cfg=None) -> dict:
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
        "unmapped_next_72h": mapping.unmapped_upcoming(cfg, con) if cfg else None,
        "misjoin_rate": q("SELECT * FROM v_misjoin_rate"),
        "redaction_last_24h": q("SELECT a.provider, a.endpoint, x.rule, count(DISTINCT x.raw_id), sum(x.n)"
                                " FROM raw_redaction x JOIN raw_response r ON r.raw_id = x.raw_id"
                                " JOIN api_request a ON a.request_id = r.request_id WHERE r.received_at_ms > ?"
                                " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3", day),
        "redaction_alerts_last_24h": q("SELECT count(*) FROM v_redaction_alerts WHERE received_at_ms > ?", day)[0][0],
        "damaged_unrepaired": len(__import__("collector.archive_tools", fromlist=["damaged"]).damaged(con)),
    }


if __name__ == "__main__":
    sys.exit(main())
