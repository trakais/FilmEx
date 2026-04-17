"""Pure argv construction — no subprocess invocation."""

from __future__ import annotations

from pathlib import Path

import pytest

from filmex.services import exiftool as et


def _intent(**kwargs) -> et.WriteIntent:
    defaults = dict(
        camera_make="FED",
        camera_model="2b",
        lens_model="Industar-50 50mm f/3.5",
        hierarchical_keywords=["Film|Portra 400", "Roll|R001"],
        flat_keywords=["Portra 400", "R001"],
    )
    defaults.update(kwargs)
    return et.WriteIntent(**defaults)


def test_detect_mode():
    assert et.detect_mode("photo.jpg") == "embed"
    assert et.detect_mode("photo.jpeg") == "embed"
    assert et.detect_mode("photo.JPG") == "embed"
    assert et.detect_mode("photo.tif") == "embed"
    assert et.detect_mode("photo.tiff") == "embed"
    assert et.detect_mode("photo.arw") == "sidecar"
    assert et.detect_mode("photo.dng") == "sidecar"
    with pytest.raises(NotImplementedError):
        et.detect_mode("photo.heic")


def test_sidecar_path_for_is_lightroom_form():
    # Replace-extension form, what Lightroom / Bridge expect.
    assert et.sidecar_path_for(Path("/a/b/photo.arw")) == Path("/a/b/photo.xmp")
    assert et.sidecar_path_for("photo.dng") == Path("photo.xmp")


def test_legacy_sidecar_path_for_is_appended_form():
    # Pre-v1 FilmEx wrote this; read path still tolerates it.
    assert et.legacy_sidecar_path_for(Path("/a/b/photo.arw")) == Path("/a/b/photo.arw.xmp")
    assert et.legacy_sidecar_path_for("photo.dng") == Path("photo.dng.xmp")


def test_find_existing_sidecar_prefers_canonical(tmp_path):
    raw = tmp_path / "frame_000.dng"
    raw.write_bytes(b"")
    canonical = tmp_path / "frame_000.xmp"
    legacy = tmp_path / "frame_000.dng.xmp"

    # Nothing on disk yet.
    assert et.find_existing_sidecar(raw) is None

    # Only legacy present — return it so re-apply edits that file, not a new one.
    legacy.write_text("<x:xmpmeta/>")
    assert et.find_existing_sidecar(raw) == legacy

    # When both exist, canonical wins (it's what Lightroom just wrote).
    canonical.write_text("<x:xmpmeta/>")
    assert et.find_existing_sidecar(raw) == canonical


def test_embed_argv_contains_gear_and_keywords():
    argv = et.build_argv("photo.jpg", _intent())
    assert argv[0] == "exiftool"
    assert "-Make=FED" in argv
    assert "-Model=2b" in argv
    assert "-LensModel=Industar-50 50mm f/3.5" in argv
    assert argv[-1] == "photo.jpg"
    # Both IPTC and XMP flat forms are written for JPG.
    assert any(a.startswith("-IPTC:Keywords+=") for a in argv)
    assert any(a.startswith("-XMP-dc:Subject+=") for a in argv)
    assert any(a.startswith("-XMP-lr:HierarchicalSubject+=") for a in argv)


def test_embed_argv_uses_remove_then_add_for_idempotency():
    argv = et.build_argv("photo.jpg", _intent())
    # Every `+=` for a list tag should be preceded by a matching `-=`.
    sub_minus = [i for i, a in enumerate(argv) if a.startswith("-XMP-dc:Subject-=")]
    sub_plus = [i for i, a in enumerate(argv) if a.startswith("-XMP-dc:Subject+=")]
    assert len(sub_minus) == 1 and len(sub_plus) == 1
    assert sub_minus[0] < sub_plus[0]


def test_sidecar_create_uses_o_and_raw_source():
    argv = et.build_argv("photo.arw", _intent(), sidecar_exists=False)
    assert "-o" in argv
    # Canonical Lightroom form: photo.arw → photo.xmp (not photo.arw.xmp).
    assert "photo.xmp" in argv
    assert "photo.arw.xmp" not in argv
    assert argv[-1] == "photo.arw"
    # IPTC:Keywords is dropped for sidecars — it's not an XMP concept.
    assert not any(a.startswith("-IPTC:Keywords") for a in argv)


def test_sidecar_update_edits_xmp_directly():
    argv = et.build_argv("photo.arw", _intent(), sidecar_exists=True)
    assert "-o" not in argv
    # Default sidecar path is the canonical replaced-extension form.
    assert argv[-1] == "photo.xmp"
    # Raw file itself must not be a target when sidecar exists.
    assert "photo.arw" not in argv
    assert not any(a.startswith("-IPTC:Keywords") for a in argv)


def test_sidecar_path_override_edits_legacy_named_xmp():
    # When the runner finds a legacy `photo.arw.xmp` on disk it pins that path
    # via sidecar_path= so we keep editing the same file Lightroom is reading,
    # rather than silently seeding a second canonical-named sidecar alongside.
    argv = et.build_argv(
        "photo.arw",
        _intent(),
        sidecar_exists=True,
        sidecar_path="photo.arw.xmp",
    )
    assert "-o" not in argv
    assert argv[-1] == "photo.arw.xmp"
    assert "photo.xmp" not in argv


def test_dry_run_strips_overwrite_original():
    argv = et.build_argv("photo.jpg", _intent(), dry_run=True)
    assert "-overwrite_original" not in argv


def test_argv_omits_gear_when_unset():
    intent = et.WriteIntent(hierarchical_keywords=["Roll|R001"], flat_keywords=["R001"])
    argv = et.build_argv("photo.jpg", intent)
    assert not any(a.startswith("-Make=") for a in argv)
    assert not any(a.startswith("-Model=") for a in argv)
    assert not any(a.startswith("-LensModel=") for a in argv)


def test_capture_date_written_when_provided():
    intent = _intent(capture_date="2026:04:16 14:30:00")
    argv = et.build_argv("photo.jpg", intent)
    assert "-DateTimeOriginal=2026:04:16 14:30:00" in argv


def test_capture_date_not_written_when_absent():
    argv = et.build_argv("photo.jpg", _intent())
    assert not any(a.startswith("-DateTimeOriginal=") for a in argv)


# ---- diff ----


def test_diff_set_when_gear_differs():
    changes = et.diff({"Make": "Sony"}, _intent())
    by_tag = {c.tag: c for c in changes}
    assert by_tag["Make"].action == "set"
    assert by_tag["Make"].before == "Sony"
    assert by_tag["Make"].after == "FED"


def test_diff_skip_when_gear_matches():
    changes = et.diff({"Make": "FED", "Model": "2b"}, _intent())
    by_tag = {c.tag: c for c in changes}
    assert by_tag["Make"].action == "skip"
    assert by_tag["Model"].action == "skip"


def test_diff_merge_keywords_without_duplicates():
    existing = {
        "Subject": ["Portra 400"],
        "HierarchicalSubject": ["Film|Portra 400"],
    }
    changes = et.diff(existing, _intent())
    by_tag = {c.tag: c for c in changes}
    # Subject was merge because "R001" is new, but "Portra 400" stays unique.
    assert by_tag["Subject"].action == "merge"
    assert by_tag["Subject"].after == ["Portra 400", "R001"]


def test_diff_skip_when_all_keywords_already_present():
    existing = {
        "Subject": ["Portra 400", "R001"],
        "HierarchicalSubject": ["Film|Portra 400", "Roll|R001"],
    }
    changes = et.diff(existing, _intent())
    by_tag = {c.tag: c for c in changes}
    assert by_tag["Subject"].action == "skip"
    assert by_tag["HierarchicalSubject"].action == "skip"
