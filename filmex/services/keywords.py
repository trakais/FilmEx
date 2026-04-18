"""Build the hierarchical keyword taxonomy for a roll.

Every managed keyword has both a flat leaf form (for `Subject`/`Keywords`) and
a pipe-delimited hierarchical form (for `HierarchicalSubject`). Both are
written on apply — Lightroom reads both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WriteContext:
    """Input to keyword building.

    All fields are snapshots (plain strings) so the keyword list is
    reproducible from a manifest even if the database changes later.
    """

    roll_id: str
    film_display_name: Optional[str] = None
    film_type: Optional[str] = None  # e.g. "color_negative"
    camera_display_name: Optional[str] = None
    lens_display_name: Optional[str] = None
    development_provider_name: Optional[str] = None
    development_method_name: Optional[str] = None
    scan_provider_name: Optional[str] = None
    scan_method_name: Optional[str] = None
    scan_capture_camera: Optional[str] = None
    scan_capture_lens: Optional[str] = None
    extra_keywords: list[str] = field(default_factory=list)


_FILM_TYPE_LABELS = {
    "color_negative": "Color Negative",
    "bw_negative": "B&W Negative",
    "slide": "Slide",
    "other": "Other",
}

_MANAGED_HIERARCHICAL_PREFIXES = (
    "Film|",
    "FilmType|",
    "Development|Provider|",
    "Development|Method|",
    "Scan|Provider|",
    "Scan|Method|",
    "Scan|Camera|",
    "Scan|Lens|",
    "Camera|",
    "Lens|",
    "Roll|",
)


def build_hierarchical_paths(ctx: WriteContext) -> list[str]:
    """Return hierarchical paths like `Film|Kodak Portra 400`, `Roll|R001`."""
    paths: list[str] = []

    if ctx.film_display_name:
        paths.append(f"Film|{ctx.film_display_name}")
    if ctx.film_type:
        label = _FILM_TYPE_LABELS.get(ctx.film_type, ctx.film_type)
        paths.append(f"FilmType|{label}")
    if ctx.development_provider_name:
        paths.append(f"Development|Provider|{ctx.development_provider_name}")
    if ctx.development_method_name:
        paths.append(f"Development|Method|{ctx.development_method_name}")
    if ctx.scan_provider_name:
        paths.append(f"Scan|Provider|{ctx.scan_provider_name}")
    if ctx.scan_method_name:
        paths.append(f"Scan|Method|{ctx.scan_method_name}")
    if ctx.scan_capture_camera:
        paths.append(f"Scan|Camera|{ctx.scan_capture_camera}")
    if ctx.scan_capture_lens:
        paths.append(f"Scan|Lens|{ctx.scan_capture_lens}")
    if ctx.camera_display_name:
        paths.append(f"Camera|{ctx.camera_display_name}")
    if ctx.lens_display_name:
        paths.append(f"Lens|{ctx.lens_display_name}")
    if ctx.roll_id:
        paths.append(f"Roll|{ctx.roll_id}")
    for kw in ctx.extra_keywords:
        kw = kw.strip()
        if kw:
            paths.append(kw)

    # Dedup preserving order
    return _unique_preserve_order(paths)


def leaves_of(paths: list[str]) -> list[str]:
    """Extract the last segment of each pipe-delimited path, deduped."""
    out = []
    for p in paths:
        leaf = p.split("|")[-1].strip()
        if leaf:
            out.append(leaf)
    return _unique_preserve_order(out)


def merge_hierarchical(existing: list[str], new: list[str]) -> list[str]:
    """Merge two hierarchical lists, preserving existing order; dedup exact string."""
    return _unique_preserve_order(list(existing) + list(new))


def merge_flat(existing: list[str], new: list[str]) -> list[str]:
    """Same semantics as merge_hierarchical for flat keywords."""
    return _unique_preserve_order(list(existing) + list(new))


def reconcile_hierarchical(
    existing: list[str],
    new: list[str],
    *,
    replace_all_managed: bool = False,
) -> list[str]:
    """Replace selected FilmEx-managed taxonomy families, preserve everything else."""
    families = _families_to_replace(new, replace_all_managed=replace_all_managed)
    preserved = [
        path for path in existing
        if not _is_managed_hierarchical_path(path) or _managed_family_for(path) not in families
    ]
    return _unique_preserve_order(preserved + list(new))


def reconcile_flat(
    existing_flat: list[str],
    existing_hierarchical: list[str],
    new_flat: list[str],
    new_hierarchical: list[str],
    *,
    replace_all_managed: bool = False,
) -> list[str]:
    """Replace leaves for selected managed families, preserve unrelated leaves."""
    families = _families_to_replace(new_hierarchical, replace_all_managed=replace_all_managed)
    managed_leaves = set(
        leaves_of([
            path for path in existing_hierarchical
            if _is_managed_hierarchical_path(path) and _managed_family_for(path) in families
        ])
    )
    preserved = [leaf for leaf in existing_flat if leaf not in managed_leaves]
    return _unique_preserve_order(preserved + list(new_flat))


def _unique_preserve_order(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _is_managed_hierarchical_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _MANAGED_HIERARCHICAL_PREFIXES)


def _managed_family_for(path: str) -> str | None:
    for prefix in _MANAGED_HIERARCHICAL_PREFIXES:
        if path.startswith(prefix):
            return prefix
    return None


def _families_to_replace(new: list[str], *, replace_all_managed: bool) -> set[str]:
    if replace_all_managed:
        return set(_MANAGED_HIERARCHICAL_PREFIXES)
    return {family for family in (_managed_family_for(path) for path in new) if family}
