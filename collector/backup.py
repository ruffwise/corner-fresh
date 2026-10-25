"""Nightly online backup with an immediate restore test.

python -m collector.backup --config /opt/corners/config.toml
A backup that has never been restored is an untested guard, so every backup
is decompressed into a temporary file, opened through the connection
contract, integrity-checked, and its row counts compared with production.
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from .config import load
from .store import open_db

CHECK_TABLES = ("raw_response", "api_request", "capture_slot", "fixture", "fixture_state_obs",
                "outcome_obs", "provider_fixture_candidate", "quota_obs")


def backup(db_path: Path, dest_dir: Path, keep_days: int = 14) -> dict:
    src = open_db(db_path)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "snap.db"
        dst = sqlite3.connect(snap)
        src.backup(dst)
        dst.close()
        counts_src = {t: src.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in CHECK_TABLES}
        out = dest_dir / f"corners-{stamp}.db.gz"
        with open(snap, "rb") as fi, gzip.open(out, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        verified = restore_test(out, counts_src)
    cutoff = time.time() - keep_days * 86400
    for old in dest_dir.glob("corners-*.db.gz"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
    return {"backup": str(out), **verified}


def restore_test(gz_path: Path, expected_min: dict) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
    from connect import connect
    with tempfile.TemporaryDirectory() as td:
        restored = Path(td) / "restored.db"
        with gzip.open(gz_path, "rb") as fi, open(restored, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        con = connect(restored)          # contract: STRICT tables, FKs on, schema version
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise RuntimeError(f"restore test: integrity_check returned {ok!r}")
        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in CHECK_TABLES}
        short = {t: (counts[t], expected_min[t]) for t in CHECK_TABLES if counts[t] < expected_min[t]}
        if short:
            raise RuntimeError(f"restore test: restored copy has fewer rows than production: {short}")
        con.close()
    return {"restore_test": "ok", "rows": counts}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.toml")
    a = ap.parse_args(argv)
    cfg = load(a.config)
    print(backup(cfg.db_path, cfg.db_path.parent / "backups"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
