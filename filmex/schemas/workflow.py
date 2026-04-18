"""Workflow DTOs: folder scan, dry-run, apply, manifests."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class FolderScanRequest(BaseModel):
    folder: str


class FileEntry(BaseModel):
    name: str
    long_edge_px: Optional[int] = None
    has_thumbnail: bool = False


class FolderScanResponse(BaseModel):
    folder: str
    roll_id: str
    files: list[FileEntry]
    source_kind: str
    scan_tier: str  # "high-res" or "standard"
    provider_hint: Optional[str] = None
    suggested_roll_date: Optional[str] = None  # YYYY-MM-DD, from file creation time


class PreviewBuildRequest(BaseModel):
    folder: str
    invert: bool = False


class PreviewBuildResponse(BaseModel):
    folder: str
    created_files: list[str] = Field(default_factory=list)
    reused_files: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)


class DryRunRequest(BaseModel):
    folder: str
    # Camera, lens, and film are all optional. The user might only know one
    # piece of context (e.g. just the film stock for a roll of unknown
    # provenance) and tagging that alone is still useful.
    camera_id: Optional[str] = None
    lens_id: Optional[str] = None
    film_stock_id: Optional[str] = None
    development_provider_id: Optional[str] = None
    development_method_id: Optional[str] = None
    scan_provider_id: Optional[str] = None
    scan_method_id: Optional[str] = None
    extra_keywords: list[str] = Field(default_factory=list)
    file_keywords: dict[str, list[str]] = Field(default_factory=dict)
    file_lens_ids: dict[str, str] = Field(default_factory=dict)
    roll_date: Optional[str] = None  # YYYY-MM-DD; when set, written as DateTimeOriginal@noon
    roll_notes: Optional[str] = None


class TagChangeOut(BaseModel):
    tag: str
    action: str  # "set" | "merge" | "skip"
    before: Any = None
    after: Any = None


class FileDiff(BaseModel):
    name: str
    changes: list[TagChangeOut]


class DryRunResponse(BaseModel):
    folder: str
    roll_id: str
    diffs: list[FileDiff]


class ApplyRequest(DryRunRequest):
    pass


class ApplyResponse(BaseModel):
    folder: str
    roll_id: str
    manifest_path: str
    applied_files: list[str]
    errors: list[str] = Field(default_factory=list)


class ManifestResponse(BaseModel):
    roll_id: str
    manifest: dict
