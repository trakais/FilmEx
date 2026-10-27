"""Entity request/response DTOs."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Mirror the documented vocabularies in AGENTS.md (Data model → FilmStock).
# Centralised here so both the API and the CLI reject typos identically.
FilmType = Literal["color_negative", "bw_negative", "slide", "other"]
ProcessType = Literal["c41", "bw", "e6", "ecn2", "other"]

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _normalize_hex_color(value: str | None) -> str | None:
    """Accept #rgb / #rrggbb / rrggbb; return canonical lowercase #rrggbb.

    Empty string is treated as None so PATCH-style clearing works cleanly.
    """
    if value is None:
        return None
    stripped = str(value).strip()
    if not stripped:
        return None
    match = _HEX_RE.match(stripped)
    if not match:
        raise ValueError(
            f"color must be a hex string like '#bb9d70' or '#abc', got {value!r}"
        )
    body = match.group(1).lower()
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    return f"#{body}"


# ---------- Responses ----------


class CameraOut(BaseModel):
    id: str
    make: str
    model: str
    display_name: str
    short_name: str | None = None
    mount: str | None
    fixed_lens_id: str | None
    camera_type: str | None
    notes: str | None
    aliases: list[str] = Field(default_factory=list)
    favorite: bool
    archived: bool


class LensOut(BaseModel):
    id: str
    make: str
    model: str
    display_name: str
    lens_model_write_value: str
    focal_length_mm: int | None
    max_aperture: float | None
    mount: str | None
    notes: str | None
    aliases: list[str] = Field(default_factory=list)
    favorite: bool
    archived: bool


class FilmStockOut(BaseModel):
    id: str
    brand: str
    name: str
    display_name: str
    short_name: str | None = None
    box_speed_iso: int
    film_type: FilmType
    process_type: ProcessType
    notes: str | None
    aliases: list[str] = Field(default_factory=list)
    favorite: bool
    archived: bool


class ProviderOut(BaseModel):
    id: str
    name: str
    kinds: list[str]
    favorite: bool
    archived: bool


class FilmBrandOut(BaseModel):
    id: str
    name: str
    color_primary: str | None = None
    color_secondary: str | None = None


class MethodOut(BaseModel):
    id: str
    name: str
    favorite: bool
    archived: bool


# ---------- Creates ----------


class CameraCreate(BaseModel):
    make: str
    model: str
    display_name: str
    short_name: str | None = None
    mount: str | None = None
    fixed_lens_id: str | None = None
    camera_type: str | None = None
    notes: str | None = None
    favorite: bool = False
    archived: bool = False
    aliases: list[str] = Field(default_factory=list)


class LensCreate(BaseModel):
    make: str
    model: str
    display_name: str
    lens_model_write_value: str
    focal_length_mm: int | None = None
    max_aperture: float | None = Field(default=None, gt=0)
    mount: str | None = None
    notes: str | None = None
    favorite: bool = False
    archived: bool = False
    aliases: list[str] = Field(default_factory=list)


class FilmStockCreate(BaseModel):
    brand: str
    name: str
    display_name: str
    short_name: str | None = None
    box_speed_iso: int
    film_type: FilmType
    process_type: ProcessType
    notes: str | None = None
    favorite: bool = False
    archived: bool = False
    aliases: list[str] = Field(default_factory=list)


class ProviderCreate(BaseModel):
    name: str
    kinds: list[str] = Field(default_factory=list)
    favorite: bool = False
    archived: bool = False


class FilmBrandCreate(BaseModel):
    name: str
    color_primary: str | None = None
    color_secondary: str | None = None

    _normalize_colors = field_validator("color_primary", "color_secondary", mode="before")(
        lambda cls, v: _normalize_hex_color(v)
    )


class FilmBrandUpdate(BaseModel):
    name: str | None = None
    color_primary: str | None = None
    color_secondary: str | None = None

    _normalize_colors = field_validator("color_primary", "color_secondary", mode="before")(
        lambda cls, v: _normalize_hex_color(v)
    )


class MethodCreate(BaseModel):
    name: str
    favorite: bool = False
    archived: bool = False


# ---------- Updates (all fields optional) ----------


class CameraUpdate(BaseModel):
    make: str | None = None
    model: str | None = None
    display_name: str | None = None
    short_name: str | None = None
    mount: str | None = None
    fixed_lens_id: str | None = None
    camera_type: str | None = None
    notes: str | None = None
    favorite: bool | None = None
    archived: bool | None = None
    aliases: list[str] | None = None


class LensUpdate(BaseModel):
    make: str | None = None
    model: str | None = None
    display_name: str | None = None
    lens_model_write_value: str | None = None
    focal_length_mm: int | None = None
    max_aperture: float | None = Field(default=None, gt=0)
    mount: str | None = None
    notes: str | None = None
    favorite: bool | None = None
    archived: bool | None = None
    aliases: list[str] | None = None


class FilmStockUpdate(BaseModel):
    brand: str | None = None
    name: str | None = None
    display_name: str | None = None
    short_name: str | None = None
    box_speed_iso: int | None = None
    film_type: FilmType | None = None
    process_type: ProcessType | None = None
    notes: str | None = None
    favorite: bool | None = None
    archived: bool | None = None
    aliases: list[str] | None = None


class ProviderUpdate(BaseModel):
    name: str | None = None
    kinds: list[str] | None = None
    favorite: bool | None = None
    archived: bool | None = None


class MethodUpdate(BaseModel):
    name: str | None = None
    favorite: bool | None = None
    archived: bool | None = None
