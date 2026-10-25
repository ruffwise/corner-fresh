"""Schema v4 -> v5 migration: equivalence with a fresh v5, data preservation,
backup-before-migrate, rollback on failure, and no silent database creation."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "db"))
import connect as dbc  # noqa: E402
from collector.store import ENDPOINTS, PROVIDERS, DatabaseMissing, init_db, open_db  # noqa: E402


def v4_database(path: Path) -> sqlite3.Connection:
    """Exactly what the v0.4 code created on the VM: the base schema plus its seed rows."""
    con = sqlite3.connect(path, isolation_level=None)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(dbc.SCHEMA_PATH.read_text())
    con.executemany("INSERT INTO provider VALUES (?,?,?)", PROVIDERS[:2])
    v4_endpoints = [e for e in ENDPOINTS]   # unchanged between v4 and v5
    con.executemany("INSERT INTO endpoint_allowlist VALUES (?,?,?)", v4_endpoints)
    con.execute("INSERT INTO capture_slot (job, scheduled_for_ms, status, started_at_ms) VALUES ('af_fixtures', 1, 'running', 1)")
    con.execute("INSERT INTO api_request (slot_id, provider, endpoint, params_canonical) VALUES (1, 'api_football', '/fixtures', 'x')")
    con.execute("INSERT INTO raw_response (request_id, page, attempt, paged, sha256, archive_path, http_status,"
                " requested_at_ms, received_at_ms, server_date_ms, body_bytes, api_errors_json, results_count,"
                " paging_current, paging_total) VALUES (1,1,1,1,?, 'a', 200, 1, 2, 2, 10, '[]', 1, 1, 1)", ("a" * 64,))
    con.execute("INSERT INTO league VALUES (39, 2026, 'Premier League', 'England', 'League')")
    con.execute("INSERT INTO team VALUES (1,'A'),(2,'B')")
    con.execute("INSERT INTO fixture VALUES (10, 39, 2026, 'R1', 1, 2, 1, 0, 'league')")
    con.execute("INSERT INTO fixture_state_obs (fixture_id, raw_id, observed_at_ms, kickoff_ms, status_short) VALUES (10,1,2,1000,'FT')")
    con.execute("INSERT INTO outcome_obs (fixture_id, source, raw_id, recorded_at_ms, status_short, goals_home_90, goals_away_90)"
                " VALUES (10, 'api_football', 1, 3, 'FT', 2, 1)")
    assert con.execute("PRAGMA user_version").fetchone()[0] == 4
    return con


def schema_of(con) -> set:
    return {(t, n, " ".join((sql or "").split())) for t, n, sql in
            con.execute("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def test_migrated_v4_equals_fresh(tmp_path):
    old = v4_database(tmp_path / "old.db")
    dbc.apply_migrations(old)
    fresh = dbc.connect(tmp_path / "fresh.db", create=True)
    assert schema_of(old) == schema_of(fresh)
    assert old.execute("PRAGMA user_version").fetchone()[0] == fresh.execute("PRAGMA user_version").fetchone()[0] == 6
    assert old.execute("SELECT * FROM close_rule").fetchall() == fresh.execute("SELECT * FROM close_rule").fetchall()


def test_migration_preserves_rows_and_contract(tmp_path):
    old = v4_database(tmp_path / "old.db")
    before = {t: old.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
              ("raw_response", "fixture", "fixture_state_obs", "outcome_obs", "endpoint_allowlist")}
    assert dbc.apply_migrations(old) == [5, 6]
    dbc.assert_schema_contract(old)
    after = {t: old.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    assert before == after
    assert old.execute("SELECT goals_home_90, first_half_start_ms FROM outcome_obs").fetchone() == (2, None)
    assert dbc.apply_migrations(old) == []                    # idempotent


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    old = v4_database(tmp_path / "old.db")
    bad = tmp_path / "migs"
    bad.mkdir()
    (bad / "005_bad.sql").write_text("CREATE TABLE half_done (x INTEGER) STRICT;\nTHIS IS NOT SQL;\nPRAGMA user_version = 5;\n")
    (bad / "006_never.sql").write_text("PRAGMA user_version = 6;\n")
    monkeypatch.setattr(dbc, "MIGRATIONS_DIR", bad)
    with pytest.raises(sqlite3.Error):
        dbc.apply_migrations(old)
    assert old.execute("PRAGMA user_version").fetchone()[0] == 4
    assert old.execute("SELECT count(*) FROM sqlite_master WHERE name='half_done'").fetchone()[0] == 0


def test_migration_files_must_be_contiguous(tmp_path, monkeypatch):
    gap = tmp_path / "migs"
    gap.mkdir()
    (gap / "006_skip.sql").write_text("PRAGMA user_version = 6;")
    monkeypatch.setattr(dbc, "MIGRATIONS_DIR", gap)
    with pytest.raises(dbc.ConnectionContractError, match="contiguously"):
        dbc.migrations()


def test_new_code_refuses_unmigrated_db(tmp_path):
    v4_database(tmp_path / "old.db").close()
    with pytest.raises(dbc.ConnectionContractError, match="migrate first"):
        open_db(tmp_path / "old.db")


def test_open_never_creates_a_database(tmp_path):            # ABSENT database
    with pytest.raises(DatabaseMissing):
        open_db(tmp_path / "typo" / "corners.db")
    assert not (tmp_path / "typo" / "corners.db").exists()


def test_init_refuses_existing_database(tmp_path):
    init_db(tmp_path / "c.db").close()
    with pytest.raises(RuntimeError, match="refusing"):
        init_db(tmp_path / "c.db")


def test_migrate_cli_backs_up_first(tmp_path, capsys, monkeypatch):
    from collector.__main__ import main
    (tmp_path / "data").mkdir()
    v4_database(tmp_path / "data" / "corners.db").close()
    (tmp_path / "config.toml").write_text('db_path = "data/corners.db"\narchive_dir = "data/archive"\n'
                                          'review_dir = "data/review"\n[[leagues]]\nleague_id = 39\nseason = 2026\n')
    assert main(["--config", str(tmp_path / "config.toml"), "migrate"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["migrated"] == [5, 6] and first["backup_restore_test"] == "ok"
    assert "pre-migrate-v4" in first["backup"] and Path(first["backup"]).exists()
    assert main(["--config", str(tmp_path / "config.toml"), "migrate"]) == 0
    assert json.loads(capsys.readouterr().out)["migrated"] == []
    assert main(["--config", str(tmp_path / "config.toml"), "check-config"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["db_exists"] and info["db_user_version"] == 6 and info["fixtures"] == 1


def test_check_config_flags_wrong_path(tmp_path, capsys):
    from collector.__main__ import main
    (tmp_path / "config.toml").write_text('db_path = "nowhere/corners.db"\n[[leagues]]\nleague_id = 39\nseason = 2026\n')
    assert main(["--config", str(tmp_path / "config.toml"), "check-config"]) == 1
    assert not (tmp_path / "nowhere").exists()


def test_v5_database_migrates_to_v6(tmp_path):
    """The VM is at v5 now: the hotfix path is 5 -> 6 only."""
    old = v4_database(tmp_path / "old.db")
    real = dbc.MIGRATIONS_DIR
    five = tmp_path / "only5"
    five.mkdir()
    (five / "005_v05.sql").write_text((real / "005_v05.sql").read_text())
    import contextlib
    saved = dbc.SCHEMA_VERSION
    try:
        dbc.MIGRATIONS_DIR, dbc.SCHEMA_VERSION = five, 5
        dbc.apply_migrations(old)
    finally:
        dbc.MIGRATIONS_DIR, dbc.SCHEMA_VERSION = real, saved
    assert old.execute("PRAGMA user_version").fetchone()[0] == 5
    assert dbc.apply_migrations(old) == [6]
    dbc.assert_schema_contract(old)
    fresh = dbc.connect(tmp_path / "fresh.db", create=True)
    assert schema_of(old) == schema_of(fresh)
    assert old.execute("SELECT count(*) FROM personal_endpoint").fetchone()[0] == 2
