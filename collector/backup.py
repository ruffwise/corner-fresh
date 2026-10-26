"""Nightly online backup with an immediate restore test.

python -m collector.backup --config /opt/corners/config.toml
A backup that has never been restored is an untested guard, so every backup
is decompressed into a temporary file, opened through the connection
contract, integrity-checked, and its row counts compared with production.
"""
from __future__ import annotations

import argparse
import json
import os
import gzip
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from .config import load

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
from connect import connect  # noqa: E402

CHECK_TABLES = ("raw_response", "api_request", "capture_slot", "fixture", "fixture_state_obs",
                "outcome_obs", "provider_fixture_candidate", "quota_obs")


def backup(db_path: Path, dest_dir: Path, keep_days: int = 14, label: str = "") -> dict:
    """Online backup of whatever schema version is on disk (used before migrations too)."""
    if not db_path.exists():
        raise RuntimeError(f"no database at {db_path}")
    src = connect(db_path, check=False)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + (f"-{label}" if label else "")
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "snap.db"
        dst = sqlite3.connect(snap)
        src.backup(dst)
        dst.close()
        counts_src = {t: src.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in CHECK_TABLES}
        version = src.execute("PRAGMA user_version").fetchone()[0]
        out = dest_dir / f"corners-{stamp}.db.gz"
        with open(snap, "rb") as fi, gzip.open(out, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        verified = restore_test(out, counts_src, version)
    cutoff = time.time() - keep_days * 86400
    for old in dest_dir.glob("corners-*.db.gz"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
    return {"backup": str(out), **verified}


def restore_test(gz_path: Path, expected_min: dict, expected_version: int) -> dict:
    with tempfile.TemporaryDirectory() as td:
        restored = Path(td) / "restored.db"
        with gzip.open(gz_path, "rb") as fi, open(restored, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        con = connect(restored, check=False)
        got = con.execute("PRAGMA user_version").fetchone()[0]
        if got != expected_version:
            raise RuntimeError(f"restore test: restored user_version {got} != {expected_version}")
        if con.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("restore test: foreign_key_check failed on the restored copy")
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"restore test: integrity_check returned {ok!r}")
        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in CHECK_TABLES}
        short = {t: (counts[t], expected_min[t]) for t in CHECK_TABLES if counts[t] < expected_min[t]}
        if short:
            raise RuntimeError(f"restore test: restored copy has fewer rows than production: {short}")
        con.close()
    return {"restore_test": "ok", "user_version": got, "rows": counts}


STATUS_LOCAL = "last_backup.json"
STATUS_OFFBOX = "last_offbox.json"
STALE_H = 26          # nightly at 02:20 UTC, plus slack


def _write_status(dest_dir: Path, name: str, obj: dict) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / (name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    os.replace(tmp, dest_dir / name)


def _read_status(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def status(data_dir: Path, archive_dir: Path | None = None, now_s: float | None = None,
           offbox_configured: bool | None = None, first_data_s: float | None = None) -> dict:
    """Backup and off-box status for the report and the watchdog.

    The local truth is the newest backup file on disk; last_backup.json adds the
    result of the last attempt (which may have failed without producing a file).
    """
    now_s = time.time() if now_s is None else now_s
    bdir = data_dir / "backups"
    files = sorted(bdir.glob("corners-*.db.gz"), key=lambda p: p.stat().st_mtime) if bdir.exists() else []
    newest = files[-1] if files else None
    last = _read_status(bdir / STATUS_LOCAL)
    off = _read_status(bdir / STATUS_OFFBOX)
    if offbox_configured is None:
        offbox_configured = bool(os.environ.get("RCLONE_REMOTE"))
    local = {
        "newest_file": newest.name if newest else None,
        "newest_utc": _utc(newest.stat().st_mtime) if newest else None,
        "newest_age_h": round((now_s - newest.stat().st_mtime) / 3600, 1) if newest else None,
        "newest_bytes": newest.stat().st_size if newest else None,
        "last_attempt": last,
    }
    problems = []
    young = first_data_s is not None and now_s - first_data_s < STALE_H * 3600   # fresh install: none due yet
    if (newest is None and not young) or (newest is not None and local["newest_age_h"] > STALE_H):
        problems.append(f"backup:no_backup_in_{STALE_H}h")
    if last and last.get("result") != "ok":
        problems.append("backup:last_attempt_failed")
    offbox = {"configured": offbox_configured, "remote": os.environ.get("RCLONE_REMOTE") or None, "last_sync": off}
    if offbox_configured:
        if not off:
            problems.append("offbox:configured_but_never_synced")
        else:
            age_h = (now_s - off.get("finished_s", 0)) / 3600
            offbox["last_sync_age_h"] = round(age_h, 1)
            if off.get("result") != "ok":
                problems.append("offbox:last_sync_failed")
            elif age_h > STALE_H:
                problems.append(f"offbox:no_sync_in_{STALE_H}h")
    return {"local": local, "offbox": offbox, "problems": problems}


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def check_file(gz: Path) -> dict:
    """Integrity check of a backup file on its own (e.g. one downloaded from off-box):
    gunzip, integrity_check, foreign_key_check, user_version and row counts."""
    with tempfile.TemporaryDirectory() as td:
        restored = Path(td) / "restored.db"
        with gzip.open(gz, "rb") as fi, open(restored, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        con = connect(restored, check=False)
        out = {"file": str(gz), "integrity_check": con.execute("PRAGMA integrity_check").fetchone()[0],
               "foreign_key_failures": len(con.execute("PRAGMA foreign_key_check").fetchall()),
               "user_version": con.execute("PRAGMA user_version").fetchone()[0],
               "rows": {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in CHECK_TABLES}}
        con.close()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--offbox-result", choices=("ok", "failed"),
                    help="record the result of the off-box sync (called by deploy/backup.sh)")
    ap.add_argument("--detail", default="")
    ap.add_argument("--remote-archive-files", type=int)
    ap.add_argument("--copy-started", type=float, help="epoch seconds; local files newer than this are not expected remotely yet")
    ap.add_argument("--check-file", type=Path, help="check one backup file and exit")
    a = ap.parse_args(argv)
    if a.check_file:
        r = check_file(a.check_file)
        print(json.dumps(r, indent=2))
        return 0 if r["integrity_check"] == "ok" and not r["foreign_key_failures"] else 1
    cfg = load(a.config)
    bdir = cfg.db_path.parent / "backups"
    if a.offbox_result:
        cut = a.copy_started if a.copy_started is not None else time.time()
        local_files = sum(1 for f in cfg.archive_dir.glob("*/*.json.gz") if f.stat().st_mtime < cut) \
            if cfg.archive_dir.exists() else 0
        complete = None if a.remote_archive_files is None else a.remote_archive_files >= local_files
        result = a.offbox_result
        detail = a.detail
        if result == "ok" and complete is False:
            result, detail = "failed", f"remote has {a.remote_archive_files} archive files, local {local_files}"
        _write_status(bdir, STATUS_OFFBOX, {
            "result": result, "finished_s": time.time(), "finished_utc": _utc(time.time()),
            "remote": os.environ.get("RCLONE_REMOTE"), "detail": detail[:500],
            "local_archive_files": local_files, "remote_archive_files": a.remote_archive_files,
            "remote_has_every_local_file": complete})
        print(json.dumps({"offbox": result, "local_archive_files": local_files,
                          "remote_archive_files": a.remote_archive_files, "detail": detail[:500]}))
        return 0 if result == "ok" else 1
    started = time.time()
    try:
        r = backup(cfg.db_path, bdir)
    except Exception as e:
        _write_status(bdir, STATUS_LOCAL, {"result": "failed", "started_utc": _utc(started),
                                           "finished_utc": _utc(time.time()), "error": f"{type(e).__name__}: {e}"[:500]})
        raise
    _write_status(bdir, STATUS_LOCAL, {"result": "ok", "started_utc": _utc(started), "finished_utc": _utc(time.time()),
                                       "backup": Path(r["backup"]).name, "restore_test": r["restore_test"],
                                       "user_version": r["user_version"], "rows": r["rows"]})
    print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
