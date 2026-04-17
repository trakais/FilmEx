"""FilmBrand entity.

Brands are the source of truth for film-stock branding — the per-brand color
pair used by the picker tile's bookmark strip lives here, not hardcoded in JS.
Film stocks reference a brand by name; the brand row owns the palette so
adding another Kodak stock automatically inherits Kodak colors.
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Field, SQLModel

from filmex.models._base import new_uuid


class FilmBrand(SQLModel, table=True):
    __tablename__ = "film_brand"

    id: str = Field(default_factory=new_uuid, primary_key=True)
    # Unique on name (case-insensitive uniqueness enforced at the service layer
    # via a lower() lookup — SQLite lacks a portable case-insensitive UNIQUE).
    name: str = Field(index=True)
    # Two-color palette rendered as a bookmark strip on film tiles. Optional:
    # null means the tile falls back to neutral gray.
    color_primary: Optional[str] = None
    color_secondary: Optional[str] = None
