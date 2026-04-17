"""Camera entity."""

from __future__ import annotations

from typing import Optional

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel

from filmex.models._base import new_uuid


class Camera(SQLModel, table=True):
    __tablename__ = "camera"

    id: str = Field(default_factory=new_uuid, primary_key=True)
    make: str
    model: str
    display_name: str = Field(index=True)
    mount: Optional[str] = Field(default=None, index=True)
    fixed_lens_id: Optional[str] = Field(default=None, foreign_key="lens.id")
    camera_type: Optional[str] = None
    notes: Optional[str] = None
    favorite: bool = Field(default=False, index=True)
    archived: bool = Field(default=False, index=True)
    aliases: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    asset_path: Optional[str] = None
