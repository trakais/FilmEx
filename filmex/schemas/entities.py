"""Entity request/response DTOs."""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Mirror the documented vocabularies in AGENTS.md (Data model → FilmStock).
# Centralised here so both the API and the CLI reject typos identically.
FilmType = Literal["color_negative", "bw_negative", "slide", "other"]
ProcessType = Literal["c41", "bw", "e6", "ecn2", "other"]

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _normalize_hex_color(value: Optional[str]) -> Optional[str]:
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
    mount: Optional[str]
    fixed_lens_id: Optional[str]
    camera_type: Optional[str]
    notes: Optional[str]
    aliases: list[str] = Field(default_factory=list)
    favorite: bool
    archived: bool


class LensOut(BaseModel):
    id: str
    make: str
    model: str
    display_name: str
    lens_model_write_value: str
    focal_length_mm: Optional[int]
    max_aperture: Optional[float]
    mount: Optional[str]
    notes: Optional[str]
    aliases: list[str] = Field(default_factory=list)
    favorite: bool
    archived: bool


class FilmStockOut(BaseModel):
    id: str
    brand: str
    name: str
    display_name: str
    box_speed_iso: int
    film_type: FilmType
    process_type: ProcessType
    notes: Optional[str]
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
    color_primary: Optional[str] = None
    color_secondary: Optional[str] = None


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
    mount: Optional[str] = None
    fixed_lens_id: Optional[str] = None
    camera_type: Optional[str] = None
    notes: Optional[str] = None
    favorite: bool = False
    aliases: list[str] = Field(default_factory=list)


class LensCreate(BaseModel):
    make: str
    model: str
    display_name: str
    lens_model_write_value: str
    focal_length_mm: Optional[int] = None
    max_aperture: Optional[float] = Field(default=None, gt=0)
    mount: Optional[str] = None
    notes: Optional[str] = None
    favorite: bool = False
    aliases: list[str] = Field(default_factory=list)


class FilmStockCreate(BaseModel):
    brand: str
    name: str
    display_name: str
    box_speed_iso: int
    film_type: FilmType
    process_type: ProcessType
    notes: Optional[str] = None
    favorite: bool = False
    aliases: list[str] = Field(default_factory=list)


class ProviderCreate(BaseModel):
    name: str
    kinds: list[str] = Field(default_factory=list)
    favorite: bool = False


class FilmBrandCreate(BaseModel):
    name: str
    color_primary: Optional[str] = None
    color_secondary: Optional[str] = None

    _normalize_colors = field_validator("color_primary", "color_secondary", mode="before")(
        lambda cls, v: _normalize_hex_color(v)
    )


class FilmBrandUpdate(BaseModel):
    name: Optional[str] = None
    color_primary: Optional[str] = None
    color_secondary: Optional[str] = None

    _normalize_colors = field_validator("color_primary", "color_secondary", mode="before")(
        lambda cls, v: _normalize_hex_color(v)
    )


class MethodCreate(BaseModel):
    name: str
    favorite: bool = False


# ---------- Updates (all fields optional) ----------


class CameraUpdate(BaseModel):
    make: Optional[str] = None
    model: Optional[str] = None
    display_name: Optional[str] = None
    mount: Optional[str] = None
    fixed_lens_id: Optional[str] = None
    camera_type: Optional[str] = None
    notes: Optional[str] = None
    favorite: Optional[bool] = None
    archived: Optional[bool] = None
    aliases: Optional[list[str]] = None


class LensUpdate(BaseModel):
    make: Optional[str] = None
    model: Optional[str] = None
    display_name: Optional[str] = None
    lens_model_write_value: Optional[str] = None
    focal_length_mm: Optional[int] = None
    max_aperture: Optional[float] = Field(default=None, gt=0)
    mount: Optional[str] = None
    notes: Optional[str] = None
    favorite: Optional[bool] = None
    archived: Optional[bool] = None
    aliases: Optional[list[str]] = None


class FilmStockUpdate(BaseModel):
    brand: Optional[str] = None
    name: Optional[str] = None
    display_name: Optional[str] = None
    box_speed_iso: Optional[int] = None
    film_type: Optional[FilmType] = None
    process_type: Optional[ProcessType] = None
    notes: Optional[str] = None
    favorite: Optional[bool] = None
    archived: Optional[bool] = None
    aliases: Optional[list[str]] = None


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    kinds: Optional[list[str]] = None
    favorite: Optional[bool] = None
    archived: Optional[bool] = None


class MethodUpdate(BaseModel):
    name: Optional[str] = None
    favorite: Optional[bool] = None
    archived: Optional[bool] = None
