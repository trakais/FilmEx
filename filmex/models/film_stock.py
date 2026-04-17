"""FilmStock entity."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel

from filmex.models._base import new_uuid


class FilmStock(SQLModel, table=True):
    __tablename__ = "film_stock"

    id: str = Field(default_factory=new_uuid, primary_key=True)
    brand: str
    name: str
    display_name: str = Field(index=True)
    box_speed_iso: int
    film_type: str  # color_negative, bw_negative, slide, other
    process_type: str  # c41, bw, e6, ecn2, other
    notes: Optional[str] = None
    favorite: bool = Field(default=False, index=True)
    archived: bool = Field(default=False, index=True)
    aliases: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    asset_path: Optional[str] = None
