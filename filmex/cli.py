"""Command-line entry point for FilmEx.

Primarily built for LLM-assisted data entry: hand an LLM `AGENTS.md` plus the
sample batch, have it produce a JSON file that matches the schema, then run

    uv run python -m filmex.cli add-from-json path/to/batch.json

Idempotent. Rows that already exist (by natural key) are skipped with a log
line, never overwritten. Use the UI or the REST API to edit or archive.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlmodel import Session, select

from filmex.db import get_engine, init_db
from filmex.models import (
    Camera,
    DevelopmentMethod,
    FilmStock,
    Lens,
    Provider,
    ScanMethod,
)
from filmex.schemas import (
    CameraCreate,
    FilmStockCreate,
    LensCreate,
    MethodCreate,
    ProviderCreate,
)

_VALID_PROVIDER_KINDS = {"development", "scan"}
_VALID_FILM_TYPES = {"color_negative", "bw_negative", "slide", "other"}
_VALID_PROCESS_TYPES = {"c41", "bw", "e6", "ecn2", "other"}

_BATCH_SECTIONS = (
    "providers",
    "development_methods",
    "scan_methods",
    "film_stocks",
    "cameras",
    "lenses",
)


# ---------- result accounting ----------


@dataclass
class SectionReport:
    section: str
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def tally(self) -> str:
        return (
            f"{self.section}: {len(self.created)} created, "
            f"{len(self.skipped)} skipped, {len(self.errors)} errors"
        )


@dataclass
class BatchReport:
    sections: list[SectionReport] = field(default_factory=list)
    fixed_lens_linked: list[str] = field(default_factory=list)
    fixed_lens_unresolved: list[str] = field(default_factory=list)

    def has_errors(self) -> bool:
        return any(s.errors for s in self.sections) or bool(self.fixed_lens_unresolved)

    def section(self, name: str) -> SectionReport:
        for s in self.sections:
            if s.section == name:
                return s
        s = SectionReport(name)
        self.sections.append(s)
        return s


# ---------- batch apply ----------


def apply_batch(batch: dict[str, Any], *, dry_run: bool = False) -> BatchReport:
    """Apply a validated-on-the-fly batch dict to the live database.

    `dry_run` validates every row and reports what would happen without
    committing anything. Uses a single session whose transaction is rolled
    back at the end.
    """
    report = BatchReport()

    unknown = set(batch) - set(_BATCH_SECTIONS) - {"$comment", "notes"}
    if unknown:
        raise ValueError(
            f"Unknown batch section(s): {sorted(unknown)}. "
            f"Valid: {list(_BATCH_SECTIONS)}"
        )

    init_db()
    with Session(get_engine()) as session:
        # 1. Order-independent entities first.
        _process_providers(session, batch.get("providers", []), report)
        _process_methods(
            session,
            DevelopmentMethod,
            batch.get("development_methods", []),
            report.section("development_methods"),
        )
        _process_methods(
            session,
            ScanMethod,
            batch.get("scan_methods", []),
            report.section("scan_methods"),
        )
        _process_film_stocks(session, batch.get("film_stocks", []), report)

        # 2. Cameras before lenses, so lens-mount validation sees new mounts.
        pending_links = _process_cameras(session, batch.get("cameras", []), report)

        # 3. Lenses.
        _process_lenses(session, batch.get("lenses", []), report)

        # 4. Resolve camera.fixed_lens_id using lens make+model references.
        _resolve_fixed_lens_links(session, pending_links, report)

        if dry_run:
            session.rollback()
        else:
            session.commit()

    return report


# ---------- providers ----------


def _process_providers(
    session: Session, items: Iterable[dict], report: BatchReport
) -> None:
    sr = report.section("providers")
    for raw in items:
        label = _label(raw, ("name",))
        try:
            payload = ProviderCreate(**raw)
        except ValidationError as exc:
            sr.errors.append(f"{label}: {_err(exc)}")
            continue
        if not payload.kinds:
            sr.errors.append(f"{label}: provider must have at least one kind")
            continue
        unknown = set(payload.kinds) - _VALID_PROVIDER_KINDS
        if unknown:
            sr.errors.append(
                f"{label}: unknown provider kind(s) {sorted(unknown)}; "
                f"valid: development, scan"
            )
            continue
        existing = _find_provider(session, payload.name)
        if existing is not None:
            sr.skipped.append(f"{label} (already exists)")
            continue
        session.add(Provider(**payload.model_dump()))
        session.flush()
        sr.created.append(label)


def _find_provider(session: Session, name: str) -> Provider | None:
    needle = name.strip().lower()
    for row in session.exec(select(Provider)).all():
        if (row.name or "").strip().lower() == needle:
            return row
    return None


# ---------- methods (development + scan share the shape) ----------


def _process_methods(
    session: Session,
    model,
    items: Iterable[dict],
    sr: SectionReport,
) -> None:
    for raw in items:
        label = _label(raw, ("name",))
        try:
            payload = MethodCreate(**raw)
        except ValidationError as exc:
            sr.errors.append(f"{label}: {_err(exc)}")
            continue
        existing = _find_method(session, model, payload.name)
        if existing is not None:
            sr.skipped.append(f"{label} (already exists)")
            continue
        session.add(model(**payload.model_dump()))
        session.flush()
        sr.created.append(label)


def _find_method(session: Session, model, name: str):
    needle = name.strip().lower()
    for row in session.exec(select(model)).all():
        if (row.name or "").strip().lower() == needle:
            return row
    return None


# ---------- film stocks ----------


def _process_film_stocks(
    session: Session, items: Iterable[dict], report: BatchReport
) -> None:
    sr = report.section("film_stocks")
    for raw in items:
        label = _label(raw, ("brand", "name")) or _label(raw, ("display_name",))
        try:
            payload = FilmStockCreate(**raw)
        except ValidationError as exc:
            sr.errors.append(f"{label}: {_err(exc)}")
            continue
        if payload.film_type not in _VALID_FILM_TYPES:
            sr.errors.append(
                f"{label}: film_type '{payload.film_type}' invalid; "
                f"use one of {sorted(_VALID_FILM_TYPES)}"
            )
            continue
        if payload.process_type not in _VALID_PROCESS_TYPES:
            sr.errors.append(
                f"{label}: process_type '{payload.process_type}' invalid; "
                f"use one of {sorted(_VALID_PROCESS_TYPES)}"
            )
            continue
        if _find_film_stock(session, payload.brand, payload.name) is not None:
            sr.skipped.append(f"{label} (already exists)")
            continue
        session.add(FilmStock(**payload.model_dump()))
        session.flush()
        sr.created.append(label)


def _find_film_stock(session: Session, brand: str, name: str) -> FilmStock | None:
    b = brand.strip().lower()
    n = name.strip().lower()
    for row in session.exec(select(FilmStock)).all():
        if (row.brand or "").strip().lower() == b and (row.name or "").strip().lower() == n:
            return row
    return None


# ---------- cameras ----------


def _process_cameras(
    session: Session, items: Iterable[dict], report: BatchReport
) -> list[tuple[str, dict]]:
    """Insert cameras. Return deferred fixed-lens links as (camera_id, ref)."""
    sr = report.section("cameras")
    pending: list[tuple[str, dict]] = []
    for raw in items:
        label = _label(raw, ("make", "model")) or _label(raw, ("display_name",))
        fixed_ref = raw.pop("fixed_lens_ref", None)
        # Deliberately drop fixed_lens_id from batch input — resolution happens
        # via fixed_lens_ref only, to avoid LLMs inventing UUIDs.
        raw.pop("fixed_lens_id", None)
        try:
            payload = CameraCreate(**raw)
        except ValidationError as exc:
            sr.errors.append(f"{label}: {_err(exc)}")
            continue
        if _find_camera(session, payload.make, payload.model) is not None:
            sr.skipped.append(f"{label} (already exists)")
            continue
        data = payload.model_dump()
        data["mount"] = _normalize_mount(data.get("mount"))
        row = Camera(**data)
        session.add(row)
        session.flush()
        sr.created.append(label)
        if fixed_ref:
            if not isinstance(fixed_ref, dict) or "make" not in fixed_ref or "model" not in fixed_ref:
                sr.errors.append(
                    f"{label}: fixed_lens_ref must be an object with 'make' and 'model'"
                )
                continue
            pending.append((row.id, fixed_ref))
    return pending


def _find_camera(session: Session, make: str, model: str) -> Camera | None:
    mk = make.strip().lower()
    md = model.strip().lower()
    for row in session.exec(select(Camera)).all():
        if (row.make or "").strip().lower() == mk and (row.model or "").strip().lower() == md:
            return row
    return None


# ---------- lenses ----------


def _process_lenses(
    session: Session, items: Iterable[dict], report: BatchReport
) -> None:
    sr = report.section("lenses")
    for raw in items:
        label = _label(raw, ("make", "model")) or _label(raw, ("display_name",))
        try:
            payload = LensCreate(**raw)
        except ValidationError as exc:
            sr.errors.append(f"{label}: {_err(exc)}")
            continue
        mount = _normalize_mount(payload.mount)
        if mount is None:
            sr.errors.append(f"{label}: lens mount is required")
            continue
        known = _known_camera_mounts(session)
        if mount not in known:
            sr.errors.append(
                f"{label}: mount '{mount}' does not match any existing camera mount. "
                f"Add a camera with that mount first, or include one in the same batch. "
                f"Known mounts: {sorted(known) or '(none yet)'}"
            )
            continue
        if _find_lens(session, payload.make, payload.model) is not None:
            sr.skipped.append(f"{label} (already exists)")
            continue
        data = payload.model_dump()
        data["mount"] = mount
        session.add(Lens(**data))
        session.flush()
        sr.created.append(label)


def _find_lens(session: Session, make: str, model: str) -> Lens | None:
    mk = make.strip().lower()
    md = model.strip().lower()
    for row in session.exec(select(Lens)).all():
        if (row.make or "").strip().lower() == mk and (row.model or "").strip().lower() == md:
            return row
    return None


def _known_camera_mounts(session: Session) -> set[str]:
    return {
        m.strip()
        for m in session.exec(select(Camera.mount)).all()
        if m and m.strip()
    }


# ---------- fixed-lens linking ----------


def _resolve_fixed_lens_links(
    session: Session,
    pending: list[tuple[str, dict]],
    report: BatchReport,
) -> None:
    for camera_id, ref in pending:
        lens = _find_lens(session, ref.get("make", ""), ref.get("model", ""))
        label = f"{ref.get('make', '?')} {ref.get('model', '?')}"
        if lens is None:
            report.fixed_lens_unresolved.append(
                f"camera {camera_id}: fixed_lens_ref '{label}' not found"
            )
            continue
        camera = session.get(Camera, camera_id)
        if camera is None:
            continue
        camera.fixed_lens_id = lens.id
        session.add(camera)
        report.fixed_lens_linked.append(f"{camera.display_name} -> {lens.display_name}")


# ---------- export ----------


def export_database() -> dict[str, list[dict]]:
    """Dump all non-archived entities as a batch-shaped dict.

    Useful for backups, for regenerating `default_data.sqlite` from the user's
    live DB, or for feeding back into an LLM session as ground truth.
    """
    init_db()
    with Session(get_engine()) as session:
        # Build a lens id -> ref lookup so camera.fixed_lens_id can be exported
        # as fixed_lens_ref instead of a UUID.
        lens_by_id = {row.id: row for row in session.exec(select(Lens)).all()}

        cameras = []
        for c in session.exec(select(Camera)).all():
            row = {
                "make": c.make,
                "model": c.model,
                "display_name": c.display_name,
                "mount": c.mount,
                "camera_type": c.camera_type,
                "notes": c.notes,
                "favorite": c.favorite,
                "aliases": list(c.aliases or []),
            }
            if c.fixed_lens_id and c.fixed_lens_id in lens_by_id:
                lens = lens_by_id[c.fixed_lens_id]
                row["fixed_lens_ref"] = {"make": lens.make, "model": lens.model}
            cameras.append(_prune(row))

        lenses = [
            _prune(
                {
                    "make": lens.make,
                    "model": lens.model,
                    "display_name": lens.display_name,
                    "lens_model_write_value": lens.lens_model_write_value,
                    "focal_length_mm": lens.focal_length_mm,
                    "max_aperture": lens.max_aperture,
                    "mount": lens.mount,
                    "notes": lens.notes,
                    "favorite": lens.favorite,
                    "aliases": list(lens.aliases or []),
                }
            )
            for lens in session.exec(select(Lens)).all()
        ]

        film_stocks = [
            _prune(
                {
                    "brand": f.brand,
                    "name": f.name,
                    "display_name": f.display_name,
                    "box_speed_iso": f.box_speed_iso,
                    "film_type": f.film_type,
                    "process_type": f.process_type,
                    "notes": f.notes,
                    "favorite": f.favorite,
                    "aliases": list(f.aliases or []),
                }
            )
            for f in session.exec(select(FilmStock)).all()
        ]

        providers = [
            _prune(
                {
                    "name": p.name,
                    "kinds": list(p.kinds or []),
                    "favorite": p.favorite,
                }
            )
            for p in session.exec(select(Provider)).all()
        ]

        development_methods = [
            _prune({"name": m.name, "favorite": m.favorite})
            for m in session.exec(select(DevelopmentMethod)).all()
        ]
        scan_methods = [
            _prune({"name": m.name, "favorite": m.favorite})
            for m in session.exec(select(ScanMethod)).all()
        ]

    return {
        "providers": providers,
        "development_methods": development_methods,
        "scan_methods": scan_methods,
        "film_stocks": film_stocks,
        "cameras": cameras,
        "lenses": lenses,
    }


# ---------- helpers ----------


def _label(raw: dict, keys: tuple[str, ...]) -> str:
    parts = [str(raw.get(k, "")).strip() for k in keys]
    joined = " ".join(p for p in parts if p)
    return joined or "<unknown>"


def _err(exc: ValidationError) -> str:
    msgs = []
    for e in exc.errors():
        loc = ".".join(str(x) for x in e.get("loc", ()))
        msgs.append(f"{loc}: {e.get('msg', 'invalid')}")
    return "; ".join(msgs)


def _normalize_mount(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _prune(row: dict) -> dict:
    """Drop None/[] values so exports stay compact."""
    out = {}
    for k, v in row.items():
        if v is None:
            continue
        if isinstance(v, list) and not v:
            continue
        if v is False:
            # keep explicit `favorite: false` out; False is the default
            continue
        out[k] = v
    return out


# ---------- entry point ----------


def _cmd_add_from_json(args) -> int:
    path = Path(args.path)
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2
    try:
        batch = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"error: not valid JSON ({exc})", file=sys.stderr)
        return 2
    if not isinstance(batch, dict):
        print("error: top-level JSON value must be an object", file=sys.stderr)
        return 2

    try:
        report = apply_batch(batch, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    _print_report(report, dry_run=args.dry_run)
    return 1 if report.has_errors() else 0


def _cmd_export_json(args) -> int:
    data = export_database()
    text = json.dumps(data, indent=2, sort_keys=False)
    if args.output == "-":
        sys.stdout.write(text + "\n")
    else:
        Path(args.output).write_text(text + "\n")
        print(f"wrote {args.output}", file=sys.stderr)
    return 0


def _print_report(report: BatchReport, *, dry_run: bool) -> None:
    header = "DRY RUN (nothing written)" if dry_run else "Applied batch"
    print(f"=== {header} ===")
    for section in report.sections:
        print(section.tally())
        for line in section.created:
            print(f"  + {line}")
        for line in section.skipped:
            print(f"  = {line}")
        for line in section.errors:
            print(f"  ! {line}")
    if report.fixed_lens_linked:
        print(f"fixed-lens links: {len(report.fixed_lens_linked)}")
        for line in report.fixed_lens_linked:
            print(f"  * {line}")
    if report.fixed_lens_unresolved:
        print(f"fixed-lens unresolved: {len(report.fixed_lens_unresolved)}")
        for line in report.fixed_lens_unresolved:
            print(f"  ! {line}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m filmex.cli",
        description="FilmEx command-line utility.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser(
        "add-from-json",
        help="Idempotently add cameras, lenses, films, providers, and methods from a JSON batch.",
    )
    add.add_argument("path", help="Path to the JSON batch file.")
    add.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report without writing to the database.",
    )
    add.set_defaults(func=_cmd_add_from_json)

    exp = sub.add_parser(
        "export-json",
        help="Dump the live database as a batch-shaped JSON document.",
    )
    exp.add_argument(
        "-o",
        "--output",
        default="-",
        help="Output path, or '-' for stdout (default).",
    )
    exp.set_defaults(func=_cmd_export_json)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    fn: Callable[[argparse.Namespace], int] = args.func
    return fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
