"""SQLModel engine and session dependency."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from typing import Optional

from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy import event
from sqlalchemy.engine import Engine

from filmex.config import DEFAULT_DB, db_path, ensure_data_dir
from filmex.migrations import migrate_db

_engine: Optional[Engine] = None


def get_engine() -> Engine:
    """Return the process-wide engine, lazily initialized."""
    global _engine
    if _engine is None:
        ensure_data_dir()
        _bootstrap_from_default_if_missing()
        url = f"sqlite:///{db_path()}"
        _engine = create_engine(
            url, echo=False, connect_args={"check_same_thread": False}
        )
        _enable_sqlite_foreign_keys(_engine)
    return _engine


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    """SQLite ships with FK enforcement off; turn it on for every connection
    so Camera.fixed_lens_id → Lens.id is actually checked."""

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()


def _bootstrap_from_default_if_missing() -> None:
    """First-run bootstrap: copy the bundled default DB if the user has none.

    After this copy the bundled file is irrelevant — user edits live only in
    `data/filmex.sqlite`. If the bundle is missing (dev working copy before
    the build script has run), fall through silently and let `init_db` create
    an empty schema instead.
    """
    target = db_path()
    if target.exists():
        return
    if not DEFAULT_DB.is_file():
        return
    shutil.copyfile(DEFAULT_DB, target)


def reset_engine() -> None:
    """Drop the cached engine. Used by tests that swap DATA_DIR between runs."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    # Import models to register them on SQLModel.metadata before create_all.
    from filmex import models  # noqa: F401

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    migrate_db(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a SQLModel session."""
    with Session(get_engine()) as session:
        yield session
