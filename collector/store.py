"""Database access, seed rows, and the content-addressed raw archive."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "db"))
from connect import connect  # noqa: E402

PROVIDERS = [
    ("api_football", 1, "api-football v3 (fixtures, statistics, outcomes, odds)"),
    ("oddspapi", 0, "OddsPapi v4 (Pinnacle price history)"),
]
# (provider, endpoint, billable). OddsPapi billing per its "Requests & Quota" page.
ENDPOINTS = [
    ("api_football", "/status", 0),
    ("api_football", "/leagues", 1),
    ("api_football", "/fixtures", 1),
    ("api_football", "/fixtures/statistics", 1),
    ("api_football", "/odds", 1),
    ("api_football", "/odds/bookmakers", 1),
    ("api_football", "/odds/bets", 1),
    ("oddspapi", "/v4/account", 0),
    ("oddspapi", "/v4/historical-odds", 0),
    ("oddspapi", "/v4/fixtures", 1),
    ("oddspapi", "/v4/tournaments", 1),
    ("oddspapi", "/v4/markets", 1),
    ("oddspapi", "/v4/bookmakers", 1),
]


def now_ms() -> int:
    return int(time.time() * 1000)


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    con = connect(path, create=fresh)
    if fresh:
        seed(con)
    return con


def seed(con: sqlite3.Connection) -> None:
    con.execute("BEGIN")
    con.executemany("INSERT OR IGNORE INTO provider VALUES (?,?,?)", PROVIDERS)
    con.executemany("INSERT OR IGNORE INTO endpoint_allowlist VALUES (?,?,?)", ENDPOINTS)
    con.execute("COMMIT")


class Archive:
    """Writes gzip'd JSON envelopes named by the sha256 of the envelope bytes.

    The envelope holds everything needed to rebuild the raw_response row, so a
    crash between writing the file and inserting the row loses nothing.
    Secrets never enter the envelope: callers pass params WITHOUT keys.
    """

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def write(self, envelope: dict) -> tuple[str, Path]:
        data = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
        sha = hashlib.sha256(data).hexdigest()
        d = self.root / sha[:2]
        d.mkdir(exist_ok=True)
        path = d / f"{sha}.json.gz"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            with open(tmp, "wb") as fh:
                fh.write(gzip.compress(data, mtime=0))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        return sha, path

    @staticmethod
    def read(path: Path) -> dict:
        return json.loads(gzip.decompress(Path(path).read_bytes()))
