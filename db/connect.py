"""The only sanctioned way to open the database.

Asserts, rather than assumes, the connection properties every schema
invariant depends on. If any assertion fails the process exits before a
single row is written.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

MIN_SQLITE = (3, 42, 0)  # STRICT tables; unixepoch('subsec') used by freeze triggers
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 4  # PRAGMA user_version set at the end of schema.sql


class ConnectionContractError(RuntimeError):
    pass


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise ConnectionContractError(msg)


def connect(path: str | Path, *, create: bool = False) -> sqlite3.Connection:
    ver = tuple(int(x) for x in sqlite3.sqlite_version.split("."))
    _assert(ver >= MIN_SQLITE, f"SQLite {sqlite3.sqlite_version} < 3.42: STRICT or unixepoch(subsec) unsupported")

    con = sqlite3.connect(path, isolation_level=None)  # explicit BEGIN/COMMIT only
    con.execute("PRAGMA foreign_keys = ON")
    _assert(con.execute("PRAGMA foreign_keys").fetchone()[0] == 1,
            "foreign_keys did not turn on: every REFERENCES would pass vacuously")
    con.execute("PRAGMA busy_timeout = 10000")
    if str(path) != ":memory:":
        mode = con.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        _assert(mode.lower() == "wal", f"journal_mode is {mode}, expected wal")
        con.execute("PRAGMA synchronous = FULL")

    if create:
        empty = con.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] == 0
        _assert(empty, "create=True on a database that already has tables")
        con.executescript(SCHEMA_PATH.read_text())

    assert_schema_contract(con)
    return con


def assert_schema_contract(con: sqlite3.Connection) -> None:
    """Every user table must be STRICT, and the database must be non-empty.
    Checking only for non-strict tables would pass vacuously on an empty DB."""
    rows = con.execute(
        "SELECT name, strict FROM pragma_table_list WHERE schema = 'main' "
        "AND type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    _assert(len(rows) > 0, "no tables found: schema not loaded")
    loose = [name for name, strict in rows if strict != 1]
    _assert(not loose, f"non-STRICT tables: {loose}")
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    _assert(ver == SCHEMA_VERSION, f"schema user_version {ver} != expected {SCHEMA_VERSION}: migrate first")
    bad_fk = con.execute("PRAGMA foreign_key_check").fetchall()
    _assert(not bad_fk, f"foreign_key_check failures: {bad_fk[:5]}")
