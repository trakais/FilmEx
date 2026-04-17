"""Entity request/response DTOs."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


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
    film_type: str
    process_type: str
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
    film_type: str
    process_type: str
    notes: Optional[str] = None
    favorite: bool = False
    aliases: list[str] = Field(default_factory=list)


class ProviderCreate(BaseModel):
    name: str
    kinds: list[str] = Field(default_factory=list)
    favorite: bool = False


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
    film_type: Optional[str] = None
    process_type: Optional[str] = None
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
