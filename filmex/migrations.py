"""Lightweight SQLite schema migrations for user-owned local databases.

FilmEx is a local-first app with a project-local SQLite file. Using SQLite's
`PRAGMA user_version` gives us a simple upgrade path without introducing a full
migration framework yet.
"""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

CURRENT_SCHEMA_VERSION = 1


def migrate_db(engine: Engine) -> None:
    """Bring the database up to `CURRENT_SCHEMA_VERSION`.

    Fresh databases get stamped to the current version. Older databases run the
    incremental migration steps. Databases from a newer app version are refused
    explicitly so we do not silently damage user data.
    """
    current = schema_version(engine)
    if current > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than this app supports "
            f"({CURRENT_SCHEMA_VERSION})"
        )

    for version in range(current + 1, CURRENT_SCHEMA_VERSION + 1):
        _apply_migration(engine, version)
        set_schema_version(engine, version)


def schema_version(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.exec_driver_sql("PRAGMA user_version").scalar_one())


def set_schema_version(engine: Engine, version: int) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version = {int(version)}")


def _apply_migration(engine: Engine, version: int) -> None:
    if version == 1:
        _migrate_to_v1(engine)
        return
    raise RuntimeError(f"No migration registered for schema version {version}")


def _migrate_to_v1(engine: Engine) -> None:
    """Backfill the current MVP schema onto pre-versioned databases."""
    _ensure_column(engine, "camera", "mount", "VARCHAR")
    _ensure_column(engine, "camera", "fixed_lens_id", "VARCHAR")
    _ensure_column(engine, "camera", "camera_type", "VARCHAR")
    _ensure_column(engine, "camera", "notes", "VARCHAR")
    _ensure_column(engine, "camera", "aliases", "JSON")
    _ensure_column(engine, "camera", "asset_path", "VARCHAR")

    _ensure_column(engine, "lens", "focal_length_mm", "INTEGER")
    _ensure_column(engine, "lens", "max_aperture", "FLOAT")
    _ensure_column(engine, "lens", "mount", "VARCHAR")
    _ensure_column(engine, "lens", "notes", "VARCHAR")
    _ensure_column(engine, "lens", "aliases", "JSON")
    _ensure_column(engine, "lens", "asset_path", "VARCHAR")

    _ensure_column(engine, "film_stock", "notes", "VARCHAR")
    _ensure_column(engine, "film_stock", "aliases", "JSON")
    _ensure_column(engine, "film_stock", "asset_path", "VARCHAR")

    _ensure_column(engine, "provider", "kinds", "JSON")
    _ensure_column(engine, "provider", "favorite", "BOOLEAN NOT NULL DEFAULT 0")
    _ensure_column(engine, "provider", "archived", "BOOLEAN NOT NULL DEFAULT 0")

    _ensure_column(engine, "development_method", "favorite", "BOOLEAN NOT NULL DEFAULT 0")
    _ensure_column(engine, "development_method", "archived", "BOOLEAN NOT NULL DEFAULT 0")

    _ensure_column(engine, "scan_method", "favorite", "BOOLEAN NOT NULL DEFAULT 0")
    _ensure_column(engine, "scan_method", "archived", "BOOLEAN NOT NULL DEFAULT 0")


def _ensure_column(engine: Engine, table: str, column: str, definition: str) -> None:
    inspector = inspect(engine)
    if not inspector.has_table(table):
        return
    columns = {col["name"] for col in inspector.get_columns(table)}
    if column in columns:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
