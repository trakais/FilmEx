"""Entity read + CRUD routes.

Archive-not-delete: spec forbids destructive delete of user gear. Archive is
a soft flag so history manifests remain meaningful.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from filmex.db import get_session
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
    CameraOut,
    CameraUpdate,
    FilmStockCreate,
    FilmStockOut,
    FilmStockUpdate,
    LensCreate,
    LensOut,
    LensUpdate,
    MethodCreate,
    MethodOut,
    MethodUpdate,
    ProviderCreate,
    ProviderOut,
    ProviderUpdate,
)

router = APIRouter()


# ---------- Cameras ----------


@router.get("/api/cameras", response_model=list[CameraOut])
def list_cameras(
    include_archived: bool = False,
    session: Session = Depends(get_session),
) -> list[CameraOut]:
    stmt = select(Camera)
    if not include_archived:
        stmt = stmt.where(Camera.archived == False)  # noqa: E712
    rows = _sort_by_favorite_then_name(session.exec(stmt).all())
    return [CameraOut(**_camera_dict(r)) for r in rows]


@router.get("/api/camera-mounts", response_model=list[str])
def list_camera_mounts(session: Session = Depends(get_session)) -> list[str]:
    rows = session.exec(select(Camera.mount)).all()
    mounts = sorted({mount.strip() for mount in rows if mount and mount.strip()}, key=str.lower)
    return mounts


@router.post("/api/cameras", response_model=CameraOut, status_code=201)
def create_camera(
    payload: CameraCreate,
    session: Session = Depends(get_session),
) -> CameraOut:
    data = payload.model_dump()
    data["mount"] = _normalize_mount(data.get("mount"))
    if data.get("fixed_lens_id") and session.get(Lens, data["fixed_lens_id"]) is None:
        raise HTTPException(status_code=400, detail="fixed_lens_id does not exist")
    row = Camera(**data)
    session.add(row)
    session.commit()
    session.refresh(row)
    return CameraOut(**_camera_dict(row))


@router.patch("/api/cameras/{camera_id}", response_model=CameraOut)
def update_camera(
    camera_id: str,
    payload: CameraUpdate,
    session: Session = Depends(get_session),
) -> CameraOut:
    row = _get_or_404(session, Camera, camera_id, "Camera")
    updates = payload.model_dump(exclude_unset=True)
    if "mount" in updates:
        updates["mount"] = _normalize_mount(updates["mount"])
    if "fixed_lens_id" in updates and updates["fixed_lens_id"]:
        if session.get(Lens, updates["fixed_lens_id"]) is None:
            raise HTTPException(status_code=400, detail="fixed_lens_id does not exist")
    _apply_updates(row, updates)
    session.add(row)
    session.commit()
    session.refresh(row)
    return CameraOut(**_camera_dict(row))


@router.post("/api/cameras/{camera_id}/archive", response_model=CameraOut)
def archive_camera(
    camera_id: str, session: Session = Depends(get_session)
) -> CameraOut:
    return _set_archived(session, Camera, camera_id, "Camera", True, CameraOut, _camera_dict)


@router.post("/api/cameras/{camera_id}/unarchive", response_model=CameraOut)
def unarchive_camera(
    camera_id: str, session: Session = Depends(get_session)
) -> CameraOut:
    return _set_archived(session, Camera, camera_id, "Camera", False, CameraOut, _camera_dict)


# ---------- Lenses ----------


@router.get("/api/lenses", response_model=list[LensOut])
def list_lenses(
    camera_id: Optional[str] = None,
    include_archived: bool = False,
    show_all: bool = False,
    session: Session = Depends(get_session),
) -> list[LensOut]:
    """Context-aware filter by the selected camera's mount, with escape hatch."""
    stmt = select(Lens)
    if not include_archived:
        stmt = stmt.where(Lens.archived == False)  # noqa: E712

    if camera_id and not show_all:
        camera = _get_or_404(session, Camera, camera_id, "Camera")
        if camera.fixed_lens_id:
            lens = session.get(Lens, camera.fixed_lens_id)
            return [LensOut(**_lens_dict(lens))] if lens else []
        if camera.mount and camera.mount.lower() != "fixed-lens":
            stmt = stmt.where(
                (Lens.mount == camera.mount) | (Lens.mount.is_(None))  # type: ignore[union-attr]
            )

    rows = _sort_by_favorite_then_name(session.exec(stmt).all())
    return [LensOut(**_lens_dict(r)) for r in rows]


@router.post("/api/lenses", response_model=LensOut, status_code=201)
def create_lens(
    payload: LensCreate,
    session: Session = Depends(get_session),
) -> LensOut:
    data = payload.model_dump()
    mount = _normalize_mount(data.get("mount"))
    if mount is None:
        raise HTTPException(status_code=400, detail="Lens mount is required")
    _require_known_camera_mount(session, mount)
    data["mount"] = mount
    row = Lens(**data)
    session.add(row)
    session.commit()
    session.refresh(row)
    return LensOut(**_lens_dict(row))


@router.patch("/api/lenses/{lens_id}", response_model=LensOut)
def update_lens(
    lens_id: str,
    payload: LensUpdate,
    session: Session = Depends(get_session),
) -> LensOut:
    row = _get_or_404(session, Lens, lens_id, "Lens")
    updates = payload.model_dump(exclude_unset=True)
    if "mount" in updates:
        mount = _normalize_mount(updates["mount"])
        if mount is None:
            raise HTTPException(status_code=400, detail="Lens mount is required")
        if mount != row.mount:
            _require_known_camera_mount(session, mount)
        updates["mount"] = mount
    _apply_updates(row, updates)
    session.add(row)
    session.commit()
    session.refresh(row)
    return LensOut(**_lens_dict(row))


@router.post("/api/lenses/{lens_id}/archive", response_model=LensOut)
def archive_lens(lens_id: str, session: Session = Depends(get_session)) -> LensOut:
    return _set_archived(session, Lens, lens_id, "Lens", True, LensOut, _lens_dict)


@router.post("/api/lenses/{lens_id}/unarchive", response_model=LensOut)
def unarchive_lens(lens_id: str, session: Session = Depends(get_session)) -> LensOut:
    return _set_archived(session, Lens, lens_id, "Lens", False, LensOut, _lens_dict)


# ---------- Film stocks ----------


@router.get("/api/film-stocks", response_model=list[FilmStockOut])
def list_film_stocks(
    include_archived: bool = False,
    session: Session = Depends(get_session),
) -> list[FilmStockOut]:
    stmt = select(FilmStock)
    if not include_archived:
        stmt = stmt.where(FilmStock.archived == False)  # noqa: E712
    rows = _sort_by_favorite_then_name(session.exec(stmt).all())
    return [FilmStockOut(**_film_dict(r)) for r in rows]


@router.post("/api/film-stocks", response_model=FilmStockOut, status_code=201)
def create_film_stock(
    payload: FilmStockCreate,
    session: Session = Depends(get_session),
) -> FilmStockOut:
    row = FilmStock(**payload.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return FilmStockOut(**_film_dict(row))


@router.patch("/api/film-stocks/{film_id}", response_model=FilmStockOut)
def update_film_stock(
    film_id: str,
    payload: FilmStockUpdate,
    session: Session = Depends(get_session),
) -> FilmStockOut:
    row = _get_or_404(session, FilmStock, film_id, "Film stock")
    _apply_updates(row, payload.model_dump(exclude_unset=True))
    session.add(row)
    session.commit()
    session.refresh(row)
    return FilmStockOut(**_film_dict(row))


@router.post("/api/film-stocks/{film_id}/archive", response_model=FilmStockOut)
def archive_film_stock(
    film_id: str, session: Session = Depends(get_session)
) -> FilmStockOut:
    return _set_archived(session, FilmStock, film_id, "Film stock", True, FilmStockOut, _film_dict)


@router.post("/api/film-stocks/{film_id}/unarchive", response_model=FilmStockOut)
def unarchive_film_stock(
    film_id: str, session: Session = Depends(get_session)
) -> FilmStockOut:
    return _set_archived(session, FilmStock, film_id, "Film stock", False, FilmStockOut, _film_dict)


# ---------- Providers ----------


@router.get("/api/providers", response_model=list[ProviderOut])
def list_providers(
    kind: Optional[str] = None,
    include_archived: bool = False,
    session: Session = Depends(get_session),
) -> list[ProviderOut]:
    stmt = select(Provider)
    if not include_archived:
        stmt = stmt.where(Provider.archived == False)  # noqa: E712
    rows = _sort_by_favorite_then_name(session.exec(stmt).all())
    if kind:
        rows = [r for r in rows if r.has_kind(kind)]
    return [ProviderOut(**_provider_dict(r)) for r in rows]


@router.post("/api/providers", response_model=ProviderOut, status_code=201)
def create_provider(
    payload: ProviderCreate,
    session: Session = Depends(get_session),
) -> ProviderOut:
    if not payload.kinds:
        raise HTTPException(status_code=400, detail="Provider must have at least one kind")
    _validate_provider_kinds(payload.kinds)
    row = Provider(**payload.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return ProviderOut(**_provider_dict(row))


@router.patch("/api/providers/{provider_id}", response_model=ProviderOut)
def update_provider(
    provider_id: str,
    payload: ProviderUpdate,
    session: Session = Depends(get_session),
) -> ProviderOut:
    row = _get_or_404(session, Provider, provider_id, "Provider")
    updates = payload.model_dump(exclude_unset=True)
    if "kinds" in updates:
        if not updates["kinds"]:
            raise HTTPException(status_code=400, detail="Provider must have at least one kind")
        _validate_provider_kinds(updates["kinds"])
    _apply_updates(row, updates)
    session.add(row)
    session.commit()
    session.refresh(row)
    return ProviderOut(**_provider_dict(row))


@router.post("/api/providers/{provider_id}/archive", response_model=ProviderOut)
def archive_provider(
    provider_id: str, session: Session = Depends(get_session)
) -> ProviderOut:
    return _set_archived(
        session, Provider, provider_id, "Provider", True, ProviderOut, _provider_dict
    )


@router.post("/api/providers/{provider_id}/unarchive", response_model=ProviderOut)
def unarchive_provider(
    provider_id: str, session: Session = Depends(get_session)
) -> ProviderOut:
    return _set_archived(
        session, Provider, provider_id, "Provider", False, ProviderOut, _provider_dict
    )


# ---------- Development methods ----------


@router.get("/api/development-methods", response_model=list[MethodOut])
def list_development_methods(
    include_archived: bool = False,
    session: Session = Depends(get_session),
) -> list[MethodOut]:
    return _list_methods(session, DevelopmentMethod, include_archived)


@router.post("/api/development-methods", response_model=MethodOut, status_code=201)
def create_development_method(
    payload: MethodCreate,
    session: Session = Depends(get_session),
) -> MethodOut:
    return _create_method(session, DevelopmentMethod, payload)


@router.patch("/api/development-methods/{method_id}", response_model=MethodOut)
def update_development_method(
    method_id: str,
    payload: MethodUpdate,
    session: Session = Depends(get_session),
) -> MethodOut:
    return _update_method(session, DevelopmentMethod, method_id, "Development method", payload)


@router.post("/api/development-methods/{method_id}/archive", response_model=MethodOut)
def archive_development_method(
    method_id: str, session: Session = Depends(get_session)
) -> MethodOut:
    return _set_archived(
        session, DevelopmentMethod, method_id, "Development method", True, MethodOut, _method_dict
    )


@router.post("/api/development-methods/{method_id}/unarchive", response_model=MethodOut)
def unarchive_development_method(
    method_id: str, session: Session = Depends(get_session)
) -> MethodOut:
    return _set_archived(
        session, DevelopmentMethod, method_id, "Development method", False, MethodOut, _method_dict
    )


# ---------- Scan methods ----------


@router.get("/api/scan-methods", response_model=list[MethodOut])
def list_scan_methods(
    include_archived: bool = False,
    session: Session = Depends(get_session),
) -> list[MethodOut]:
    return _list_methods(session, ScanMethod, include_archived)


@router.post("/api/scan-methods", response_model=MethodOut, status_code=201)
def create_scan_method(
    payload: MethodCreate,
    session: Session = Depends(get_session),
) -> MethodOut:
    return _create_method(session, ScanMethod, payload)


@router.patch("/api/scan-methods/{method_id}", response_model=MethodOut)
def update_scan_method(
    method_id: str,
    payload: MethodUpdate,
    session: Session = Depends(get_session),
) -> MethodOut:
    return _update_method(session, ScanMethod, method_id, "Scan method", payload)


@router.post("/api/scan-methods/{method_id}/archive", response_model=MethodOut)
def archive_scan_method(
    method_id: str, session: Session = Depends(get_session)
) -> MethodOut:
    return _set_archived(
        session, ScanMethod, method_id, "Scan method", True, MethodOut, _method_dict
    )


@router.post("/api/scan-methods/{method_id}/unarchive", response_model=MethodOut)
def unarchive_scan_method(
    method_id: str, session: Session = Depends(get_session)
) -> MethodOut:
    return _set_archived(
        session, ScanMethod, method_id, "Scan method", False, MethodOut, _method_dict
    )


# ---------- helpers ----------


_VALID_PROVIDER_KINDS = {"development", "scan"}


def _validate_provider_kinds(kinds: list[str]) -> None:
    unknown = set(kinds) - _VALID_PROVIDER_KINDS
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider kind(s): {sorted(unknown)}. Valid: development, scan",
        )


def _normalize_mount(value):
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _known_camera_mounts(session: Session) -> set[str]:
    rows = session.exec(select(Camera.mount)).all()
    return {mount.strip() for mount in rows if mount and mount.strip()}


def _require_known_camera_mount(session: Session, mount: str) -> None:
    if mount in _known_camera_mounts(session):
        return
    raise HTTPException(
        status_code=400,
        detail="Lens mount must match an existing camera mount",
    )


def _sort_by_favorite_then_name(rows):
    return sorted(
        rows,
        key=lambda r: (not bool(r.favorite), (getattr(r, "display_name", None) or r.name or "").lower()),
    )


def _get_or_404(session: Session, model, entity_id: str, label: str):
    row = session.get(model, entity_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return row


def _apply_updates(row, updates: dict) -> None:
    for key, value in updates.items():
        setattr(row, key, value)


def _set_archived(
    session: Session,
    model,
    entity_id: str,
    label: str,
    archived: bool,
    out_cls,
    dict_fn,
):
    row = _get_or_404(session, model, entity_id, label)
    row.archived = archived
    session.add(row)
    session.commit()
    session.refresh(row)
    return out_cls(**dict_fn(row))


def _list_methods(session: Session, model, include_archived: bool) -> list[MethodOut]:
    stmt = select(model)
    if not include_archived:
        stmt = stmt.where(model.archived == False)  # noqa: E712
    rows = _sort_by_favorite_then_name(session.exec(stmt).all())
    return [MethodOut(**_method_dict(r)) for r in rows]


def _create_method(session: Session, model, payload: MethodCreate) -> MethodOut:
    row = model(**payload.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return MethodOut(**_method_dict(row))


def _update_method(
    session: Session, model, method_id: str, label: str, payload: MethodUpdate
) -> MethodOut:
    row = _get_or_404(session, model, method_id, label)
    _apply_updates(row, payload.model_dump(exclude_unset=True))
    session.add(row)
    session.commit()
    session.refresh(row)
    return MethodOut(**_method_dict(row))


def _camera_dict(c: Camera) -> dict:
    return {
        "id": c.id,
        "make": c.make,
        "model": c.model,
        "display_name": c.display_name,
        "mount": c.mount,
        "fixed_lens_id": c.fixed_lens_id,
        "camera_type": c.camera_type,
        "notes": c.notes,
        "aliases": list(c.aliases or []),
        "favorite": c.favorite,
        "archived": c.archived,
    }


def _lens_dict(lens: Lens) -> dict:
    return {
        "id": lens.id,
        "make": lens.make,
        "model": lens.model,
        "display_name": lens.display_name,
        "lens_model_write_value": lens.lens_model_write_value,
        "focal_length_mm": lens.focal_length_mm,
        "max_aperture": lens.max_aperture,
        "mount": lens.mount,
        "notes": lens.notes,
        "aliases": list(lens.aliases or []),
        "favorite": lens.favorite,
        "archived": lens.archived,
    }


def _film_dict(f: FilmStock) -> dict:
    return {
        "id": f.id,
        "brand": f.brand,
        "name": f.name,
        "display_name": f.display_name,
        "box_speed_iso": f.box_speed_iso,
        "film_type": f.film_type,
        "process_type": f.process_type,
        "notes": f.notes,
        "aliases": list(f.aliases or []),
        "favorite": f.favorite,
        "archived": f.archived,
    }


def _provider_dict(p: Provider) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "kinds": list(p.kinds or []),
        "favorite": p.favorite,
        "archived": p.archived,
    }


def _method_dict(m) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "favorite": m.favorite,
        "archived": m.archived,
    }
