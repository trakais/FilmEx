"""Schema versioning and lightweight SQLite migrations."""

from __future__ import annotations

import sqlite3

import pytest
from sqlmodel import SQLModel, create_engine

from filmex import models  # noqa: F401
from filmex.migrations import CURRENT_SCHEMA_VERSION, migrate_db, schema_version


def test_migrate_db_stamps_fresh_schema(tmp_path):
    path = tmp_path / "fresh.sqlite"
    engine = create_engine(f"sqlite:///{path}")

    SQLModel.metadata.create_all(engine)
    migrate_db(engine)

    assert schema_version(engine) == CURRENT_SCHEMA_VERSION


def test_migrate_db_backfills_legacy_columns(tmp_path):
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE camera (
            id VARCHAR PRIMARY KEY NOT NULL,
            make VARCHAR NOT NULL,
            model VARCHAR NOT NULL,
            display_name VARCHAR NOT NULL,
            favorite BOOLEAN NOT NULL DEFAULT 0,
            archived BOOLEAN NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    migrate_db(engine)

    with sqlite3.connect(path) as check:
        columns = {
            row[1] for row in check.execute("PRAGMA table_info(camera)").fetchall()
        }
        version = check.execute("PRAGMA user_version").fetchone()[0]

    assert {"mount", "fixed_lens_id", "camera_type", "notes", "aliases", "asset_path"} <= columns
    assert version == CURRENT_SCHEMA_VERSION


def test_migrate_db_rejects_newer_schema(tmp_path):
    path = tmp_path / "future.sqlite"
    engine = create_engine(f"sqlite:///{path}")

    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA user_version = 999")

    with pytest.raises(RuntimeError, match="newer than this app supports"):
        migrate_db(engine)
