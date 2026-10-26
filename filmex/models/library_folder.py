"""Cached library index rows, so the Library view opens instantly and still
shows something while the NAS is unmounted."""

from __future__ import annotations

from sqlmodel import Field, SQLModel


class LibraryFolderRow(SQLModel, table=True):
    __tablename__ = "library_folder"

    path: str = Field(primary_key=True)
    root: str = Field(index=True)
    parent: str | None = None
    name: str
    display_name: str
    depth: int
    status: str
    image_count: int = 0
    untagged_count: int = 0
    sidecar_count: int = 0
    kinds_json: str = "{}"
    has_manifest: bool = False
    roll_id: str | None = None
    film_display: str | None = None
    camera_display: str | None = None
    last_applied_at: str | None = None
    is_flat_year: bool = False
    scanned_at: str
