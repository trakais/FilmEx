"""Unified Provider entity plus the two coarse method enums.

A single Provider row carries one or more `kinds` values. A lab usually offers
both "development" and "scan"; "Home" is development-only; "Self" is scan-only.
"""

from __future__ import annotations

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel

from filmex.models._base import new_uuid


class Provider(SQLModel, table=True):
    __tablename__ = "provider"

    id: str = Field(default_factory=new_uuid, primary_key=True)
    name: str = Field(index=True)
    # Stored as JSON list of strings. Semantic set, but SQLite doesn't have sets.
    kinds: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    favorite: bool = Field(default=False, index=True)
    archived: bool = Field(default=False, index=True)

    def has_kind(self, kind: str) -> bool:
        return kind in (self.kinds or [])


class DevelopmentMethod(SQLModel, table=True):
    __tablename__ = "development_method"

    id: str = Field(default_factory=new_uuid, primary_key=True)
    name: str = Field(index=True)
    favorite: bool = Field(default=False, index=True)
    archived: bool = Field(default=False, index=True)


class ScanMethod(SQLModel, table=True):
    __tablename__ = "scan_method"

    id: str = Field(default_factory=new_uuid, primary_key=True)
    name: str = Field(index=True)
    favorite: bool = Field(default=False, index=True)
    archived: bool = Field(default=False, index=True)
