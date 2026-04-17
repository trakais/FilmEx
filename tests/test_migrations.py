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


def test_migrate_db_v3_creates_film_brand_and_backfills(tmp_path):
    """v3 pulls colors out of film_stock and into a dedicated film_brand table.

    A pre-v3 database with v2 color columns on film_stock (`color_primary`,
    `color_secondary`) should end up with:
      - a new `film_brand` table, one row per distinct `film_stock.brand`
      - known brands seeded with the canonical palette
      - the v2 color columns dropped from film_stock
    """
    path = tmp_path / "v2.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE film_stock (
            id VARCHAR PRIMARY KEY NOT NULL,
            brand VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            display_name VARCHAR NOT NULL,
            box_speed_iso INTEGER NOT NULL,
            film_type VARCHAR NOT NULL,
            process_type VARCHAR NOT NULL,
            favorite BOOLEAN NOT NULL DEFAULT 0,
            archived BOOLEAN NOT NULL DEFAULT 0,
            color_primary VARCHAR,
            color_secondary VARCHAR
        );
        INSERT INTO film_stock
            (id, brand, name, display_name, box_speed_iso, film_type, process_type)
        VALUES
            ('s1', 'Kodak',     'Gold 200',          'Kodak Gold 200',        200, 'color_negative', 'c41'),
            ('s2', 'Kodak',     'Portra 400',        'Kodak Portra 400',      400, 'color_negative', 'c41'),
            ('s3', 'Lomography','Color Negative 400','Lomography CN 400',     400, 'color_negative', 'c41'),
            ('s4', 'WeirdoBrand','Experimental 50',  'WeirdoBrand Exp 50',     50, 'color_negative', 'c41');
        PRAGMA user_version = 2;
        """
    )
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{path}")
    migrate_db(engine)

    with sqlite3.connect(path) as check:
        # film_brand exists with the backfilled rows
        brand_rows = check.execute(
            "SELECT name, color_primary, color_secondary FROM film_brand"
        ).fetchall()
        brand_map = {name: (cp, cs) for name, cp, cs in brand_rows}

        # Obsolete v2 columns were dropped from film_stock
        fs_columns = {
            row[1] for row in check.execute("PRAGMA table_info(film_stock)").fetchall()
        }
        version = check.execute("PRAGMA user_version").fetchone()[0]

    assert {"Kodak", "Lomography", "WeirdoBrand"} <= set(brand_map.keys())
    # Known brands get seed colors; unknowns land with nulls.
    assert brand_map["Kodak"] == ("#f7ad03", "#c9489e")
    assert brand_map["Lomography"] == ("#bb9d70", "#292825")
    assert brand_map["WeirdoBrand"] == (None, None)

    assert "color_primary" not in fs_columns
    assert "color_secondary" not in fs_columns
    assert version == CURRENT_SCHEMA_VERSION


def test_migrate_db_v3_is_idempotent(tmp_path):
    """Re-running v3 on an already-migrated DB doesn't duplicate brand rows."""
    path = tmp_path / "fresh.sqlite"
    engine = create_engine(f"sqlite:///{path}")
    SQLModel.metadata.create_all(engine)
    migrate_db(engine)

    # Insert one stock + brand manually, then re-run migrate_db. v3 should
    # notice the brand already exists (case-insensitive) and skip.
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO film_brand (id, name, color_primary, color_secondary) "
            "VALUES ('b1', 'Kodak', '#f7ad03', '#c9489e')"
        )
        conn.execute(
            "INSERT INTO film_stock (id, brand, name, display_name, "
            "box_speed_iso, film_type, process_type, favorite, archived) "
            "VALUES ('s1', 'kodak', 'Gold 200', 'Kodak Gold 200', 200, "
            "'color_negative', 'c41', 0, 0)"
        )
        conn.commit()

    # Force a re-run of the v3 step by rolling back the version stamp.
    with engine.begin() as c:
        c.exec_driver_sql("PRAGMA user_version = 2")
    migrate_db(engine)

    with sqlite3.connect(path) as check:
        kodak_count = check.execute(
            "SELECT COUNT(*) FROM film_brand WHERE lower(name) = 'kodak'"
        ).fetchone()[0]
    assert kodak_count == 1


def test_migrate_db_v4_repairs_missing_brand_rows_and_palettes(tmp_path):
    path = tmp_path / "v3-broken.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE film_brand (
            id VARCHAR PRIMARY KEY NOT NULL,
            name VARCHAR NOT NULL,
            color_primary VARCHAR,
            color_secondary VARCHAR
        );
        CREATE INDEX ix_film_brand_name ON film_brand(name);
        CREATE TABLE film_stock (
            id VARCHAR PRIMARY KEY NOT NULL,
            brand VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            display_name VARCHAR NOT NULL,
            box_speed_iso INTEGER NOT NULL,
            film_type VARCHAR NOT NULL,
            process_type VARCHAR NOT NULL,
            favorite BOOLEAN NOT NULL DEFAULT 0,
            archived BOOLEAN NOT NULL DEFAULT 0
        );
        INSERT INTO film_brand (id, name, color_primary, color_secondary)
        VALUES ('b1', 'Kodak', NULL, '#c9489e');
        INSERT INTO film_stock
            (id, brand, name, display_name, box_speed_iso, film_type, process_type)
        VALUES
            ('s1', 'Kodak', 'Gold 200', 'Kodak Gold 200', 200, 'color_negative', 'c41'),
            ('s2', 'Ferrania', 'P30', 'Ferrania P30', 80, 'bw_negative', 'bw');
        PRAGMA user_version = 3;
        """
    )
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{path}")
    migrate_db(engine)

    with sqlite3.connect(path) as check:
        brand_rows = check.execute(
            "SELECT name, color_primary, color_secondary FROM film_brand"
        ).fetchall()
        version = check.execute("PRAGMA user_version").fetchone()[0]

    brand_map = {name: (cp, cs) for name, cp, cs in brand_rows}
    assert brand_map["Kodak"] == ("#f7ad03", "#c9489e")
    assert brand_map["Ferrania"] == (None, None)
    assert version == CURRENT_SCHEMA_VERSION


def test_migrate_db_rejects_newer_schema(tmp_path):
    path = tmp_path / "future.sqlite"
    engine = create_engine(f"sqlite:///{path}")

    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA user_version = 999")

    with pytest.raises(RuntimeError, match="newer than this app supports"):
        migrate_db(engine)
