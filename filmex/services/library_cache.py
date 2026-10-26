"""Persistence for the library index (table ``library_folder``)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, delete, select

from filmex.models import LibraryFolderRow
from filmex.services.library import LibraryFolder


def _now() -> str:
    return datetime.now(UTC).isoformat()


def replace(session: Session, root: str, rows: list[LibraryFolder]) -> str:
    """Replace every cached row for ``root`` in one transaction."""
    scanned_at = _now()
    session.exec(delete(LibraryFolderRow).where(LibraryFolderRow.root == root))
    for row in rows:
        session.add(
            LibraryFolderRow(
                path=row.path,
                root=root,
                parent=row.parent,
                name=row.name,
                display_name=row.display_name,
                depth=row.depth,
                status=row.status,
                image_count=row.image_count,
                untagged_count=row.untagged_count,
                sidecar_count=row.sidecar_count,
                kinds_json=json.dumps(row.kinds),
                has_manifest=row.has_manifest,
                roll_id=row.roll_id,
                film_display=row.film_display,
                camera_display=row.camera_display,
                last_applied_at=row.last_applied_at,
                is_flat_year=row.is_flat_year,
                scanned_at=scanned_at,
            )
        )
    session.commit()
    return scanned_at


def rows_for(session: Session, root: str) -> list[dict[str, Any]]:
    rows = session.exec(select(LibraryFolderRow).where(LibraryFolderRow.root == root)).all()
    out = []
    for row in rows:
        data = row.model_dump()
        data["kinds"] = json.loads(data.pop("kinds_json") or "{}")
        data.pop("root", None)
        out.append(data)
    out.sort(
        key=lambda r: (
            (r["parent"] or r["display_name"]).lower(),
            r["depth"],
            r["display_name"].lower(),
        )
    )
    return out


def scanned_at(session: Session, root: str) -> str | None:
    row = session.exec(
        select(LibraryFolderRow.scanned_at).where(LibraryFolderRow.root == root).limit(1)
    ).first()
    return row
