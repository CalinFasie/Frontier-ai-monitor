from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine, inspect, text

from frontier_monitor.db import Database, metadata
from frontier_monitor.migrations import DEFAULT_MIGRATIONS_DIR, MigrationError, apply_migrations


def _migration_dir(path, *files):
    path.mkdir()
    for filename, contents in files:
        (path / filename).write_text(contents, encoding="utf-8")
    return path


def test_fresh_sqlite_database_applies_coverage_migration(tmp_path):
    db = Database(f"sqlite:///{tmp_path / 'monitor.sqlite'}")

    inspector = inspect(db.engine)
    assert {
        "runs",
        "sources",
        "developments",
        "development_sources",
        "observations",
        "candidate_decisions",
        "briefs",
    }.issubset(set(inspector.get_table_names()))
    assert inspector.has_table("schema_migrations")
    assert inspector.has_table("coverage_state")
    assert "coverage_state" not in metadata.tables

    with db.engine.connect() as cx:
        rows = cx.execute(
            text("SELECT version, filename, checksum, applied_at FROM schema_migrations")
        ).mappings().all()
    assert len(rows) == 1
    assert rows[0]["version"] == 1
    assert rows[0]["filename"] == "001_coverage_state.sql"
    migration_bytes = (DEFAULT_MIGRATIONS_DIR / "001_coverage_state.sql").read_bytes()
    assert rows[0]["checksum"] == hashlib.sha256(migration_bytes).hexdigest()
    assert rows[0]["applied_at"] is not None
    db.engine.dispose()


def test_initializing_database_twice_is_idempotent(tmp_path):
    url = f"sqlite:///{tmp_path / 'monitor.sqlite'}"
    first = Database(url)
    first.engine.dispose()
    second = Database(url)

    with second.engine.connect() as cx:
        rows = cx.execute(text("SELECT version, filename FROM schema_migrations")).all()
    assert rows == [(1, "001_coverage_state.sql")]
    second.engine.dispose()


def test_metadata_create_all_does_not_create_migration_managed_table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'baseline.sqlite'}", future=True)
    metadata.create_all(engine)
    assert not inspect(engine).has_table("coverage_state")

    apply_migrations(engine)
    assert inspect(engine).has_table("coverage_state")
    engine.dispose()


def test_applied_migration_checksum_is_immutable(tmp_path):
    migrations_dir = _migration_dir(
        tmp_path / "migrations",
        ("001_example.sql", "CREATE TABLE example (id INTEGER PRIMARY KEY);"),
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'checksum.sqlite'}", future=True)

    apply_migrations(engine, migrations_dir)
    (migrations_dir / "001_example.sql").write_text(
        "CREATE TABLE example (id INTEGER PRIMARY KEY, value TEXT);", encoding="utf-8"
    )

    with pytest.raises(MigrationError, match="Checksum mismatch.*immutable"):
        apply_migrations(engine, migrations_dir)
    engine.dispose()


def test_failed_migration_and_history_insert_roll_back_on_sqlite(tmp_path):
    migrations_dir = _migration_dir(
        tmp_path / "migrations",
        ("001_example.sql", "CREATE TABLE example (id INTEGER PRIMARY KEY);"),
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'rollback.sqlite'}", future=True)
    with engine.begin() as cx:
        cx.exec_driver_sql(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY NOT NULL CHECK (version <> 1), "
            "filename TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL, "
            "applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )

    with pytest.raises(MigrationError, match="Failed to apply migration 001_example.sql"):
        apply_migrations(engine, migrations_dir)

    assert not inspect(engine).has_table("example")
    with engine.connect() as cx:
        assert cx.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one() == 0
    engine.dispose()


def test_invalid_migration_sql_is_not_recorded(tmp_path):
    migrations_dir = _migration_dir(tmp_path / "migrations", ("001_broken.sql", "THIS IS NOT SQL;"))
    engine = create_engine(f"sqlite:///{tmp_path / 'failed.sqlite'}", future=True)

    with pytest.raises(MigrationError, match="Failed to apply migration 001_broken.sql"):
        apply_migrations(engine, migrations_dir)

    with engine.connect() as cx:
        assert cx.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one() == 0
    engine.dispose()


def test_malformed_and_duplicate_versions_fail_before_application(tmp_path):
    malformed_dir = _migration_dir(tmp_path / "malformed", ("coverage.sql", "SELECT 1;"))
    engine = create_engine(f"sqlite:///{tmp_path / 'names.sqlite'}", future=True)
    with pytest.raises(MigrationError, match="Malformed migration filename"):
        apply_migrations(engine, malformed_dir)

    duplicate_dir = _migration_dir(
        tmp_path / "duplicate",
        ("001_one.sql", "SELECT 1;"),
        ("1_two.sql", "SELECT 2;"),
    )
    with pytest.raises(MigrationError, match="Duplicate migration version 1"):
        apply_migrations(engine, duplicate_dir)
    engine.dispose()
