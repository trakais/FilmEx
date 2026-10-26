"""Lightweight SQLite schema migrations for user-owned local databases.

FilmEx is a local-first app with a project-local SQLite file. Using SQLite's
`PRAGMA user_version` gives us a simple upgrade path without introducing a full
migration framework yet.
"""

from __future__ import annotations

import uuid

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

CURRENT_SCHEMA_VERSION = 8


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
    if version == 2:
        _migrate_to_v2(engine)
        return
    if version == 3:
        _migrate_to_v3(engine)
        return
    if version == 4:
        _migrate_to_v4(engine)
        return
    if version == 5:
        _migrate_to_v5(engine)
        return
    if version == 6:
        _migrate_to_v6(engine)
        return
    if version == 7:
        _migrate_to_v7(engine)
        return
    if version == 8:
        _migrate_to_v8(engine)
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


def _migrate_to_v2(engine: Engine) -> None:
    """Intermediate per-stock color columns.

    Obsoleted by v3: colors moved to the `film_brand` entity. These columns
    linger on any database that passed through v2 but are no longer read or
    written. v3 drops them to keep the schema tidy.
    """
    _ensure_column(engine, "film_stock", "color_primary", "VARCHAR")
    _ensure_column(engine, "film_stock", "color_secondary", "VARCHAR")


# Known brand palettes seeded on first creation of the `film_brand` table.
# Derived from the physical film-box design — a dominant color and an accent.
# User can edit any of these later via Settings; nothing here is load-bearing
# for correctness, only for tile aesthetics.
_KNOWN_BRAND_COLORS: dict[str, tuple[str, str]] = {
    "Kodak": ("#f7ad03", "#c9489e"),
    "Kodak Eastman": ("#f31a2b", "#fdb71b"),
    "Foma": ("#2e4076", "#f5d300"),
    "Fomapan": ("#c9489e", "#f1d100"),
    "Ilford": ("#000000", "#ffffff"),
    "Kentmere": ("#2f2298", "#e50163"),
    "Agfa": ("#73767c", "#cf1c30"),
    "Fujifilm": ("#079952", "#942b6f"),
    "Harman": ("#d22b19", "#ffffff"),
    "CineStill": ("#493ab4", "#171918"),
    "Rollei": ("#000000", "#df3a46"),
    "Lomography": ("#bb9d70", "#292825"),
}


def _migrate_to_v3(engine: Engine) -> None:
    """Introduce the `film_brand` entity.

    Brands are now a first-class row that owns the two-color palette for the
    film picker. Existing `film_stock.brand` strings get a corresponding brand
    row via backfill. Known brands get pre-populated colors; unknown brands
    land with null colors until the user or an LLM batch sets them.
    """
    _sync_film_brands(engine)

    # Drop obsolete v2 color columns from film_stock if they were ever added.
    # SQLite supports DROP COLUMN since 3.35 (2021-03); we can rely on it.
    inspector = inspect(engine)
    film_stock_cols = (
        {col["name"] for col in inspector.get_columns("film_stock")}
        if inspector.has_table("film_stock")
        else set()
    )
    for col in ("color_primary", "color_secondary"):
        if col in film_stock_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"ALTER TABLE film_stock DROP COLUMN {col}")


def _migrate_to_v4(engine: Engine) -> None:
    """Repair/complete film-brand backfill on already-versioned databases.

    Some working copies may already be stamped at v3 while still missing
    backfilled brand rows or known-brand palettes. Re-running the reconciliation
    step is safe and makes the migration self-healing.
    """
    _sync_film_brands(engine)


def _migrate_to_v5(engine: Engine) -> None:
    """Add the `app_setting` key/value table.

    `init_db` runs `create_all` before migrations, so on most databases the
    table already exists by the time this runs. Creating it here as well keeps
    the migration self-contained for databases upgraded by other paths.
    """
    inspector = inspect(engine)
    if inspector.has_table("app_setting"):
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE app_setting (
                key VARCHAR PRIMARY KEY NOT NULL,
                value_json VARCHAR NOT NULL,
                updated_at VARCHAR NOT NULL
            )
            """
        )


def _migrate_to_v6(engine: Engine) -> None:
    """Add the `library_folder` cache table (see services/library_cache.py)."""
    inspector = inspect(engine)
    if inspector.has_table("library_folder"):
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE library_folder (
                path VARCHAR PRIMARY KEY NOT NULL,
                root VARCHAR NOT NULL,
                parent VARCHAR,
                name VARCHAR NOT NULL,
                display_name VARCHAR NOT NULL,
                depth INTEGER NOT NULL,
                status VARCHAR NOT NULL,
                image_count INTEGER NOT NULL DEFAULT 0,
                untagged_count INTEGER NOT NULL DEFAULT 0,
                sidecar_count INTEGER NOT NULL DEFAULT 0,
                kinds_json VARCHAR NOT NULL DEFAULT '{}',
                has_manifest BOOLEAN NOT NULL DEFAULT 0,
                roll_id VARCHAR,
                film_display VARCHAR,
                camera_display VARCHAR,
                last_applied_at VARCHAR,
                is_flat_year BOOLEAN NOT NULL DEFAULT 0,
                scanned_at VARCHAR NOT NULL
            )
            """
        )
        conn.exec_driver_sql("CREATE INDEX ix_library_folder_root ON library_folder(root)")


def _migrate_to_v7(engine: Engine) -> None:
    """Short names for cameras and film stocks, used by the folder naming system."""
    _ensure_column(engine, "camera", "short_name", "VARCHAR")
    _ensure_column(engine, "film_stock", "short_name", "VARCHAR")


def _migrate_to_v8(engine: Engine) -> None:
    """Operation log for batch file operations (renames, splits, imports)."""
    inspector = inspect(engine)
    if inspector.has_table("operation_log"):
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE operation_log (
                id VARCHAR PRIMARY KEY NOT NULL,
                kind VARCHAR NOT NULL,
                status VARCHAR NOT NULL,
                root VARCHAR,
                started_at VARCHAR NOT NULL,
                finished_at VARCHAR,
                undone_at VARCHAR,
                plan_json VARCHAR NOT NULL DEFAULT '{}',
                result_json VARCHAR NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.exec_driver_sql("CREATE INDEX ix_operation_log_kind ON operation_log(kind)")
        conn.exec_driver_sql("CREATE INDEX ix_operation_log_status ON operation_log(status)")


def _sync_film_brands(engine: Engine) -> None:
    """Ensure film_brand exists and covers every distinct film_stock brand.

    Known brands get their canonical palette when the row is first created. For
    existing rows, missing colors are backfilled one side at a time without
    overwriting user edits.
    """
    inspector = inspect(engine)

    if not inspector.has_table("film_brand"):
        with engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE film_brand (
                    id VARCHAR PRIMARY KEY NOT NULL,
                    name VARCHAR NOT NULL,
                    color_primary VARCHAR,
                    color_secondary VARCHAR
                )
                """
            )
            conn.exec_driver_sql("CREATE INDEX ix_film_brand_name ON film_brand(name)")

    if not inspector.has_table("film_stock"):
        return

    with engine.begin() as conn:
        brand_rows = conn.exec_driver_sql(
            "SELECT id, name, color_primary, color_secondary FROM film_brand"
        ).fetchall()
        brand_map = {
            name.strip().lower(): (brand_id, name, color_primary, color_secondary)
            for brand_id, name, color_primary, color_secondary in brand_rows
            if name and name.strip()
        }
        stock_brands = {
            row[0].strip()
            for row in conn.exec_driver_sql(
                "SELECT DISTINCT brand FROM film_stock"
            ).fetchall()
            if row[0] and row[0].strip()
        }

        for brand in sorted(stock_brands):
            colors = _KNOWN_BRAND_COLORS.get(brand, (None, None))
            current = brand_map.get(brand.lower())
            if current is None:
                conn.exec_driver_sql(
                    "INSERT INTO film_brand (id, name, color_primary, color_secondary) "
                    "VALUES (?, ?, ?, ?)",
                    (str(uuid.uuid4()), brand, colors[0], colors[1]),
                )
                continue

            brand_id, _, color_primary, color_secondary = current
            next_primary = color_primary if color_primary is not None else colors[0]
            next_secondary = color_secondary if color_secondary is not None else colors[1]
            if next_primary == color_primary and next_secondary == color_secondary:
                continue
            conn.exec_driver_sql(
                "UPDATE film_brand SET color_primary = ?, color_secondary = ? WHERE id = ?",
                (next_primary, next_secondary, brand_id),
            )


def _ensure_column(engine: Engine, table: str, column: str, definition: str) -> None:
    inspector = inspect(engine)
    if not inspector.has_table(table):
        return
    columns = {col["name"] for col in inspector.get_columns(table)}
    if column in columns:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
