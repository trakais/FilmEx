"""Lens entity."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel

from filmex.models._base import new_uuid


class Lens(SQLModel, table=True):
    __tablename__ = "lens"

    id: str = Field(default_factory=new_uuid, primary_key=True)
    make: str
    model: str
    display_name: str = Field(index=True)
    lens_model_write_value: str
    focal_length_mm: Optional[int] = None
    max_aperture: Optional[float] = None
    mount: Optional[str] = Field(default=None, index=True)
    notes: Optional[str] = None
    favorite: bool = Field(default=False, index=True)
    archived: bool = Field(default=False, index=True)
    aliases: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    asset_path: Optional[str] = None
