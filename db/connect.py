"""The only sanctioned way to open the database.

Asserts, rather than assumes, the connection properties every schema
invariant depends on. If any assertion fails the process exits before a
single row is written.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

MIN_SQLITE = (3, 42, 0)  # STRICT tables; unixepoch('subsec') used by freeze triggers
SCHEMA_PATH = Path(__file__).with_name("schema.sql")          # frozen v4 base
MIGRATIONS_DIR = Path(__file__).with_name("migrations")       # NNN_name.sql, each ends with PRAGMA user_version = NNN
BASE_VERSION = 4
SCHEMA_VERSION = 7


def migrations() -> list[tuple[int, Path]]:
    out = sorted((int(p.name.split("_", 1)[0]), p) for p in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
    versions = [v for v, _ in out]
    if versions != list(range(BASE_VERSION + 1, BASE_VERSION + 1 + len(out))) or (out and versions[-1] != SCHEMA_VERSION):
        raise ConnectionContractError(f"migration files {versions} do not run contiguously from {BASE_VERSION + 1} "
                                      f"to {SCHEMA_VERSION}")
    return out


class ConnectionContractError(RuntimeError):
    pass


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise ConnectionContractError(msg)


def connect(path: str | Path, *, create: bool = False, check: bool = True) -> sqlite3.Connection:
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
        apply_migrations(con)

    if check:
        assert_schema_contract(con)
    return con


def apply_migrations(con: sqlite3.Connection) -> list[int]:
    """Run every pending migration, each in its own transaction. Returns versions applied."""
    done = []
    current = con.execute("PRAGMA user_version").fetchone()[0]
    _assert(current >= BASE_VERSION, f"database at user_version {current}: older than the v{BASE_VERSION} base")
    for version, path in migrations():
        if version <= current:
            continue
        try:
            con.executescript("BEGIN;\n" + path.read_text() + "\nCOMMIT;")
        except sqlite3.Error:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        got = con.execute("PRAGMA user_version").fetchone()[0]
        _assert(got == version, f"migration {path.name} left user_version {got}, expected {version}")
        done.append(version)
    return done


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
