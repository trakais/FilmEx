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
    assert any(a.startswith("-IPTC:Keywords=") for a in argv)
    assert any(a.startswith("-XMP-dc:Subject=") for a in argv)
    assert any(a.startswith("-XMP-lr:HierarchicalSubject=") for a in argv)


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


def test_description_written_when_provided():
    argv = et.build_argv("photo.jpg", _intent(description="Pushed two stops"))
    assert "-XMP-dc:Description=Pushed two stops" in argv
    assert "-IPTC:Caption-Abstract=Pushed two stops" in argv


def test_sidecar_description_omits_iptc_caption():
    argv = et.build_argv("photo.arw", _intent(description="Roll note"), sidecar_exists=False)
    assert "-XMP-dc:Description=Roll note" in argv
    assert not any(a.startswith("-IPTC:Caption-Abstract=") for a in argv)


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


def test_diff_sets_description():
    changes = et.diff({"Description": "Old"}, _intent(description="New note"))
    by_tag = {c.tag: c for c in changes}
    assert by_tag["Description"].action == "set"
    assert by_tag["Description"].after == "New note"


def test_resolve_intent_replaces_old_managed_keywords_but_keeps_user_keywords():
    existing = {
        "Subject": ["Kodak Portra 400", "BFS", "Anna"],
        "HierarchicalSubject": [
            "Film|Kodak Portra 400",
            "Development|Provider|BFS",
            "Scan|Provider|BFS",
            "People|Anna",
        ],
    }
    intent = et.WriteIntent(
        hierarchical_keywords=[
            "Film|Fujifilm Fujicolor C200",
            "Development|Provider|Agenskalns Fuji",
            "Scan|Provider|Agenskalns Fuji",
            "Roll|R001",
        ],
        flat_keywords=["Fujifilm Fujicolor C200", "Agenskalns Fuji", "R001"],
    )
    resolved = et.resolve_intent(existing, intent)
    assert resolved.hierarchical_keywords == [
        "People|Anna",
        "Film|Fujifilm Fujicolor C200",
        "Development|Provider|Agenskalns Fuji",
        "Scan|Provider|Agenskalns Fuji",
        "Roll|R001",
    ]
    assert resolved.flat_keywords == [
        "Anna",
        "Fujifilm Fujicolor C200",
        "Agenskalns Fuji",
        "R001",
    ]


def test_resolve_intent_preserves_other_managed_families_when_not_replacing_all():
    existing = {
        "Subject": ["Old Film", "Old Lens", "Anna"],
        "HierarchicalSubject": [
            "Film|Old Film",
            "Lens|Old Lens",
            "People|Anna",
        ],
    }
    intent = et.WriteIntent(
        hierarchical_keywords=["Development|Provider|Lab Two"],
        flat_keywords=["Lab Two"],
    )
    resolved = et.resolve_intent(existing, intent)
    assert resolved.hierarchical_keywords == [
        "Film|Old Film",
        "Lens|Old Lens",
        "People|Anna",
        "Development|Provider|Lab Two",
    ]
    assert resolved.flat_keywords == ["Old Film", "Old Lens", "Anna", "Lab Two"]


def test_resolve_intent_replace_managed_clears_owned_scalars_when_unset():
    existing = {
        "Make": "Nikon",
        "Model": "F3",
        "LensModel": "Nikkor 50mm",
        "Subject": ["Anna", "Old Film"],
        "HierarchicalSubject": ["People|Anna", "Film|Old Film"],
        "DateTimeOriginal": "2021:05:01 12:00:00",
        "Description": "Old note",
    }
    resolved = et.resolve_intent(existing, et.WriteIntent(), replace_managed=True)
    assert resolved.camera_make == ""
    assert resolved.camera_model == ""
    assert resolved.lens_model == ""
    assert resolved.capture_date == ""
    assert resolved.description == ""
    assert resolved.hierarchical_keywords == ["People|Anna"]
    assert resolved.flat_keywords == ["Anna"]
