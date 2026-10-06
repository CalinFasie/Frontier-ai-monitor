from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "sql" / "migrations"
_MIGRATION_NAME = re.compile(r"(?P<version>[0-9]+)_[a-z0-9][a-z0-9_-]*\.sql\Z")
_CREATE_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY NOT NULL,
    filename TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


class MigrationError(RuntimeError):
    """Raised when repository migration history cannot be applied safely."""


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    checksum: str
    sql: str


def _discover_migrations(migrations_dir: Path) -> list[Migration]:
    if not migrations_dir.is_dir():
        raise MigrationError(f"Migration directory does not exist: {migrations_dir}")

    migrations: list[Migration] = []
    seen_versions: dict[int, str] = {}
    files = sorted(
        (path for path in migrations_dir.iterdir() if path.is_file() and path.name.lower().endswith(".sql")),
        key=lambda path: path.name,
    )
    for path in files:
        match = _MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise MigrationError(
                f"Malformed migration filename {path.name!r}; expected <version>_<name>.sql"
            )
        version = int(match.group("version"))
        if version < 1:
            raise MigrationError(f"Migration version must be positive: {path.name}")
        if version in seen_versions:
            raise MigrationError(
                f"Duplicate migration version {version}: {seen_versions[version]} and {path.name}"
            )
        seen_versions[version] = path.name

        try:
            raw = path.read_bytes()
            sql = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MigrationError(f"Could not read migration {path.name}: {exc}") from exc
        if not sql.strip():
            raise MigrationError(f"Migration file is empty: {path.name}")
        migrations.append(
            Migration(
                version=version,
                filename=path.name,
                checksum=hashlib.sha256(raw).hexdigest(),
                sql=sql,
            )
        )
    return sorted(migrations, key=lambda migration: (migration.version, migration.filename))


def _ensure_history_table(engine: Engine) -> None:
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(_CREATE_HISTORY_TABLE)
    except SQLAlchemyError as exc:
        raise MigrationError(f"Could not initialize schema_migrations: {exc}") from exc


def _read_applied(engine: Engine) -> dict[int, dict[str, str]]:
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT version, filename, checksum FROM schema_migrations ORDER BY version")
            ).mappings().all()
    except SQLAlchemyError as exc:
        raise MigrationError(f"Could not read schema_migrations: {exc}") from exc
    return {
        int(row["version"]): {"filename": str(row["filename"]), "checksum": str(row["checksum"])}
        for row in rows
    }


def apply_migrations(engine: Engine, migrations_dir: Path | None = None) -> None:
    """Apply immutable, numbered SQL files and record each successful file.

    SQLite's legacy transaction mode does not automatically begin a transaction
    for DDL. Explicitly begin one so the migration and its history row roll back
    together. Other supported dialects use SQLAlchemy's normal transaction.
    """
    directory = Path(migrations_dir) if migrations_dir is not None else DEFAULT_MIGRATIONS_DIR
    migrations = _discover_migrations(directory)
    _ensure_history_table(engine)

    applied = _read_applied(engine)
    by_version = {migration.version: migration for migration in migrations}
    for version, record in applied.items():
        migration = by_version.get(version)
        if migration is None:
            raise MigrationError(
                f"Applied migration version {version} ({record['filename']}) is missing from {directory}"
            )
        if record["filename"] != migration.filename:
            raise MigrationError(
                f"Applied migration {version} was renamed from {record['filename']} to {migration.filename}"
            )
        if record["checksum"] != migration.checksum:
            raise MigrationError(
                f"Checksum mismatch for applied migration {migration.filename}; "
                "applied migration files are immutable"
            )

    highest_applied = max(applied, default=0)
    pending = [migration for migration in migrations if migration.version not in applied]
    out_of_order = [migration for migration in pending if migration.version <= highest_applied]
    if out_of_order:
        names = ", ".join(migration.filename for migration in out_of_order)
        raise MigrationError(
            f"Pending migrations must have versions greater than the highest applied version "
            f"{highest_applied}: {names}"
        )

    for migration in pending:
        try:
            with engine.begin() as connection:
                if connection.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN")
                connection.exec_driver_sql(migration.sql)
                connection.execute(
                    text(
                        "INSERT INTO schema_migrations (version, filename, checksum) "
                        "VALUES (:version, :filename, :checksum)"
                    ),
                    {
                        "version": migration.version,
                        "filename": migration.filename,
                        "checksum": migration.checksum,
                    },
                )
        except SQLAlchemyError as exc:
            raise MigrationError(
                f"Failed to apply migration {migration.filename} (version {migration.version}): {exc}"
            ) from exc
