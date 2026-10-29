"""Roll manifest read/write.

The manifest lives next to the images as `roll_manifest.json`. Snapshots of
gear/provider/method fields are stored inline so the manifest remains
understandable even if the database schema changes later.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

MANIFEST_FILENAME = "roll_manifest.json"
MANIFEST_VERSION = 1
# What happened to the folder itself: a re-apply keeps these as they are.
FOLDER_HISTORY_KEYS = ("renames", "import_source", "gathered_renditions", "linked_scans")


@dataclass
class GearSnapshot:
    id: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    display_name: Optional[str] = None
    mount: Optional[str] = None
    lens_model_write_value: Optional[str] = None


@dataclass
class FilmSnapshot:
    id: Optional[str] = None
    brand: Optional[str] = None
    display_name: Optional[str] = None
    box_speed_iso: Optional[int] = None
    film_type: Optional[str] = None
    process_type: Optional[str] = None


@dataclass
class NamedSnapshot:
    id: Optional[str] = None
    name: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def manifest_path_for(folder: Path | str) -> Path:
    return Path(folder) / MANIFEST_FILENAME


def read_manifest(folder: Path | str) -> Optional[dict]:
    path = manifest_path_for(folder)
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_manifest(folder: Path | str, data: dict) -> Path:
    path = manifest_path_for(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def build_new_manifest(
    *,
    folder: Path,
    roll_id: str,
    source_kind: str,
    scan_tier: str,
    camera: GearSnapshot,
    lens: GearSnapshot,
    film: FilmSnapshot,
    development_provider: NamedSnapshot,
    development_method: NamedSnapshot,
    scan_provider: NamedSnapshot,
    scan_method: NamedSnapshot,
    capture_date_value: Optional[str] = None,
    capture_date_precision: str = "unknown",
    capture_date_source: Optional[str] = None,
    notes: Optional[str] = None,
    place: Optional[str] = None,
) -> dict:
    return {
        "manifest_version": MANIFEST_VERSION,
        "roll_id": roll_id,
        "created_at": _now(),
        "updated_at": _now(),
        "source_kind": source_kind,
        "scan_tier": scan_tier,
        "folder_relative_path": folder.name,
        "analog_camera_snapshot": asdict(camera),
        "analog_lens_snapshot": asdict(lens),
        "film_stock_snapshot": asdict(film),
        "development_provider_snapshot": asdict(development_provider),
        "development_method_snapshot": asdict(development_method),
        "scan_provider_snapshot": asdict(scan_provider),
        "scan_method_snapshot": asdict(scan_method),
        "scan_capture_camera": None,
        "scan_capture_lens": None,
        "capture_date_value": capture_date_value,
        "capture_date_precision": capture_date_precision,
        "capture_date_source": capture_date_source,
        "notes": notes,
        "place": place,
        "file_overrides": {},
        "apply_history": [],
    }


def append_apply_history(
    manifest: dict,
    *,
    applied_files: list[str],
    diffs: list[dict],
    dry_run: bool = False,
) -> dict:
    entry = {
        "timestamp": _now(),
        "dry_run": dry_run,
        "applied_files": list(applied_files),
        "diffs": diffs,
    }
    manifest.setdefault("apply_history", []).append(entry)
    manifest["updated_at"] = _now()
    return manifest


def set_file_overrides(
    manifest: dict,
    *,
    extra_keywords_by_file: dict[str, list[str]],
    lens_by_file: dict[str, GearSnapshot],
) -> dict:
    """Replace the manifest's per-file override section with current values."""
    normalized: dict[str, dict[str, Any]] = {}
    names = set(extra_keywords_by_file) | set(lens_by_file)
    for name in names:
        entry: dict[str, Any] = {}
        keywords = extra_keywords_by_file.get(name, [])
        cleaned = [kw.strip() for kw in keywords if kw and kw.strip()]
        if cleaned:
            entry["extra_keywords"] = cleaned
        lens = lens_by_file.get(name)
        if lens and any(getattr(lens, field) for field in ("id", "make", "model", "display_name")):
            entry["lens_snapshot"] = asdict(lens)
        if entry:
            normalized[name] = entry
    manifest["file_overrides"] = normalized
    manifest["updated_at"] = _now()
    return manifest


def merge_existing_manifest(
    existing: dict,
    fresh: dict,
) -> dict:
    """Re-apply: keep created_at, apply_history and the folder's own history
    (renames, import, gathered renditions, linked lab scans); refresh
    snapshots + updated_at."""
    merged = dict(fresh)
    merged["created_at"] = existing.get("created_at", merged["created_at"])
    merged["apply_history"] = list(existing.get("apply_history", []))
    for key in FOLDER_HISTORY_KEYS:
        if key in existing:
            merged[key] = existing[key]
    return merged


