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
    assert old.execute("PRAGMA user_version").fetchone()[0] == fresh.execute("PRAGMA user_version").fetchone()[0] == 8
    assert old.execute("SELECT * FROM close_rule").fetchall() == fresh.execute("SELECT * FROM close_rule").fetchall()


def test_migration_preserves_rows_and_contract(tmp_path):
    old = v4_database(tmp_path / "old.db")
    before = {t: old.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in
              ("raw_response", "fixture", "fixture_state_obs", "outcome_obs", "endpoint_allowlist")}
    assert dbc.apply_migrations(old) == [5, 6, 7, 8]
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
    (bad / "007_never.sql").write_text("PRAGMA user_version = 7;\n")
    (bad / "008_never.sql").write_text("PRAGMA user_version = 8;\n")
    monkeypatch.setattr(dbc, "MIGRATIONS_DIR", bad)
    with pytest.raises(sqlite3.Error):
        dbc.apply_migrations(old)
    assert old.execute("PRAGMA user_version").fetchone()[0] == 4
    assert old.execute("SELECT count(*) FROM sqlite_master WHERE name='half_done'").fetchone()[0] == 0


def test_migration_files_must_be_contiguous(tmp_path, monkeypatch):
    gap = tmp_path / "migs"
    gap.mkdir()
    (gap / "009_skip.sql").write_text("PRAGMA user_version = 9;")
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
                                          'review_dir = "data/review"\nactive_jobs = ["af_odds"]\n[[leagues]]\nleague_id = 39\nseason = 2026\n')
    assert main(["--config", str(tmp_path / "config.toml"), "migrate"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["migrated"] == [5, 6, 7, 8] and first["backup_restore_test"] == "ok"
    assert "pre-migrate-v4" in first["backup"] and Path(first["backup"]).exists()
    assert main(["--config", str(tmp_path / "config.toml"), "migrate"]) == 0
    assert json.loads(capsys.readouterr().out)["migrated"] == []
    assert main(["--config", str(tmp_path / "config.toml"), "check-config"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["db_exists"] and info["db_user_version"] == 8 and info["fixtures"] == 1


def test_check_config_flags_wrong_path(tmp_path, capsys):
    from collector.__main__ import main
    (tmp_path / "config.toml").write_text('db_path = "nowhere/corners.db"\nactive_jobs = ["af_odds"]\n[[leagues]]\nleague_id = 39\nseason = 2026\n')
    assert main(["--config", str(tmp_path / "config.toml"), "check-config"]) == 1
    assert not (tmp_path / "nowhere").exists()


def test_v5_database_migrates_to_current(tmp_path):
    """A v5 database (the state before the v0.5.1 hotfix) still migrates: 5 -> 6 -> 7."""
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
    assert dbc.apply_migrations(old) == [6, 7, 8]
    dbc.assert_schema_contract(old)
    fresh = dbc.connect(tmp_path / "fresh.db", create=True)
    assert schema_of(old) == schema_of(fresh)
    assert old.execute("SELECT count(*) FROM personal_endpoint").fetchone()[0] == 2


def test_v6_database_migrates_to_v7(tmp_path):
    """The VM is at v6 now: the v0.5.4 path is 6 -> 7 only, and keeps every row."""
    old = v4_database(tmp_path / "old.db")
    real = dbc.MIGRATIONS_DIR
    six = tmp_path / "upto6"
    six.mkdir()
    for f in ("005_v05.sql", "006_v051.sql"):
        (six / f).write_text((real / f).read_text())
    saved = dbc.SCHEMA_VERSION
    try:
        dbc.MIGRATIONS_DIR, dbc.SCHEMA_VERSION = six, 6
        dbc.apply_migrations(old)
    finally:
        dbc.MIGRATIONS_DIR, dbc.SCHEMA_VERSION = real, saved
    before = {t: old.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("raw_response", "api_request", "capture_slot", "fixture")}
    assert dbc.apply_migrations(old) == [7, 8]
    dbc.assert_schema_contract(old)
    assert before == {t: old.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    assert schema_of(old) == schema_of(dbc.connect(tmp_path / "fresh.db", create=True))
    assert [r[0] for r in old.execute("SELECT user_version FROM schema_event ORDER BY 1")] == [6, 7, 8]


def test_v7_to_v8_rebuild_keeps_mappings_children_and_triggers(tmp_path):
    """008 rebuilds provider_fixture_map (to add map_basis). Mapping rows, their
    mapping_audit children, the guards, and the triggers on OTHER tables that name the
    table must all survive."""
    old = v4_database(tmp_path / "old.db")
    real = dbc.MIGRATIONS_DIR
    seven = tmp_path / "upto7"
    seven.mkdir()
    for f in ("005_v05.sql", "006_v051.sql", "007_v054.sql"):
        (seven / f).write_text((real / f).read_text())
    saved = dbc.SCHEMA_VERSION
    try:
        dbc.MIGRATIONS_DIR, dbc.SCHEMA_VERSION = seven, 7
        dbc.apply_migrations(old)
    finally:
        dbc.MIGRATIONS_DIR, dbc.SCHEMA_VERSION = real, saved
    e = old.execute
    e("INSERT INTO provider_team_map VALUES ('oddspapi','h','A',1,?), ('oddspapi','a','B',2,?)", ("c" * 40, "c" * 40))
    e("INSERT INTO provider_fixture_candidate (provider, provider_fixture_id, raw_id, tournament_key, home_team_key,"
      " away_team_key, home_team_name, away_team_name, start_ms) VALUES ('oddspapi','p1',1,'17','h','a','A','B',1000)")
    e("INSERT INTO provider_fixture_map (provider, provider_fixture_id, fixture_id, home_team_key, away_team_key,"
      " provider_start_ms, kickoff_at_map_ms, evidence, mapped_at_ms) VALUES ('oddspapi','p1',10,'h','a',1000,1000,'ev',5)")
    e("INSERT INTO mapping_audit (provider, provider_fixture_id, method, agrees, detail, evidence_raw_id, recorded_at_ms)"
      " VALUES ('oddspapi','p1','kickoff_recheck',1,'ok',1,6)")
    assert dbc.apply_migrations(old) == [8]
    dbc.assert_schema_contract(old)
    assert old.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert e("SELECT provider_fixture_id, map_basis FROM provider_fixture_map").fetchall() == [("p1", "kickoff")]
    assert e("SELECT count(*) FROM mapping_audit").fetchone()[0] == 1
    assert e("PRAGMA foreign_key_check").fetchall() == []
    assert schema_of(old) == schema_of(dbc.connect(tmp_path / "fresh.db", create=True))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        e("DELETE FROM provider_fixture_map")
    with pytest.raises(sqlite3.IntegrityError, match="price_history: raw response is not usable"):
        e("INSERT INTO price_history (parse_run_id, raw_id, provider, fixture_id, provider_fixture_id, bookmaker_id,"
          " provider_market_label, value_label, market_key, selection, line_q, valid_from_ms, price, active)"
          " VALUES (1, 999, 'oddspapi', 10, 'p1', 1, 'x', 'x', 'x', 'x', 0, 1, 2.0, 1)")   # the trigger still resolves


def test_failed_rebuild_rolls_back_and_restores_foreign_keys(tmp_path, monkeypatch):
    old = v4_database(tmp_path / "old.db")
    migs = tmp_path / "migs"
    migs.mkdir()
    real = dbc.MIGRATIONS_DIR
    for f in ("005_v05.sql", "006_v051.sql", "007_v054.sql"):
        (migs / f).write_text((real / f).read_text())
    (migs / "008_bad.sql").write_text("-- corners-migration: foreign_keys=off\nDROP TABLE provider_fixture_map;\n"
                                      "NOT SQL;\nPRAGMA user_version = 8;\n")
    monkeypatch.setattr(dbc, "MIGRATIONS_DIR", migs)
    with pytest.raises(sqlite3.Error):
        dbc.apply_migrations(old)
    assert old.execute("PRAGMA user_version").fetchone()[0] == 7
    assert old.execute("SELECT count(*) FROM sqlite_master WHERE name='provider_fixture_map'").fetchone()[0] == 1
    assert old.execute("PRAGMA foreign_keys").fetchone()[0] == 1
