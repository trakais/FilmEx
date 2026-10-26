"""Key/value application settings persisted in the user's database."""

from __future__ import annotations

from sqlmodel import Field, SQLModel


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_setting"

    key: str = Field(primary_key=True)
    value_json: str
    updated_at: str
