"""Keyword taxonomy construction."""

from __future__ import annotations

from filmex.services import keywords as kw


def _ctx(**overrides):
    defaults = dict(
        roll_id="R001",
        film_display_name="Kodak Portra 400",
        film_type="color_negative",
        camera_display_name="FED 2b",
        lens_display_name="Industar-50 50mm f/3.5",
        development_provider_name="BFS",
        development_method_name="C-41",
        scan_provider_name="Self",
        scan_method_name="DSLR capture",
    )
    defaults.update(overrides)
    return kw.WriteContext(**defaults)


def test_builds_expected_hierarchical_paths():
    paths = kw.build_hierarchical_paths(_ctx())
    assert "Film|Kodak Portra 400" in paths
    assert "FilmType|Color Negative" in paths
    assert "Development|Provider|BFS" in paths
    assert "Development|Method|C-41" in paths
    assert "Scan|Provider|Self" in paths
    assert "Scan|Method|DSLR capture" in paths
    assert "Camera|FED 2b" in paths
    assert "Lens|Industar-50 50mm f/3.5" in paths
    assert "Roll|R001" in paths


def test_film_type_label_mapping():
    paths = kw.build_hierarchical_paths(_ctx(film_type="bw_negative"))
    assert "FilmType|B&W Negative" in paths


def test_unknown_film_type_falls_through():
    paths = kw.build_hierarchical_paths(_ctx(film_type="super8"))
    assert "FilmType|super8" in paths


def test_optional_fields_skipped_when_none():
    ctx = _ctx(
        lens_display_name=None,
        development_provider_name=None,
        scan_capture_camera=None,
    )
    paths = kw.build_hierarchical_paths(ctx)
    assert not any(p.startswith("Lens|") for p in paths)
    assert not any(p.startswith("Development|Provider|") for p in paths)
    assert not any(p.startswith("Scan|Camera|") for p in paths)


def test_extra_keywords_included_and_stripped():
    ctx = _ctx(extra_keywords=["  Portrait ", "", "Warm Light"])
    paths = kw.build_hierarchical_paths(ctx)
    assert "Portrait" in paths
    assert "Warm Light" in paths
    assert "" not in paths


def test_leaves_of_extracts_last_segment():
    leaves = kw.leaves_of(["Film|Kodak Portra 400", "Development|Provider|BFS", "Roll|R001"])
    assert leaves == ["Kodak Portra 400", "BFS", "R001"]


def test_merge_preserves_existing_order_and_dedups():
    existing = ["A", "B", "C"]
    new = ["C", "D", "A"]
    assert kw.merge_flat(existing, new) == ["A", "B", "C", "D"]
    assert kw.merge_hierarchical(existing, new) == ["A", "B", "C", "D"]


def test_reconcile_replaces_managed_taxonomy_and_preserves_unmanaged_keywords():
    existing_h = [
        "Film|Kodak Portra 400",
        "Development|Provider|BFS",
        "Scan|Provider|BFS",
        "People|Anna",
    ]
    new_h = [
        "Film|Fujifilm Fujicolor C200",
        "Development|Provider|Agenskalns Fuji",
        "Scan|Provider|Agenskalns Fuji",
        "Roll|R001",
    ]
    assert kw.reconcile_hierarchical(existing_h, new_h) == [
        "People|Anna",
        "Film|Fujifilm Fujicolor C200",
        "Development|Provider|Agenskalns Fuji",
        "Scan|Provider|Agenskalns Fuji",
        "Roll|R001",
    ]

    existing_flat = ["Kodak Portra 400", "BFS", "Anna"]
    new_flat = ["Fujifilm Fujicolor C200", "Agenskalns Fuji", "R001"]
    assert kw.reconcile_flat(existing_flat, existing_h, new_flat, new_h) == [
        "Anna",
        "Fujifilm Fujicolor C200",
        "Agenskalns Fuji",
        "R001",
    ]


def test_reconcile_preserves_unmentioned_managed_families_by_default():
    existing_h = [
        "Film|Kodak Portra 400",
        "Lens|Industar-50 50mm f/3.5",
        "People|Anna",
    ]
    new_h = ["Development|Provider|BFS"]
    assert kw.reconcile_hierarchical(existing_h, new_h) == [
        "Film|Kodak Portra 400",
        "Lens|Industar-50 50mm f/3.5",
        "People|Anna",
        "Development|Provider|BFS",
    ]


def test_reconcile_replace_all_managed_clears_absent_families_too():
    existing_h = [
        "Film|Kodak Portra 400",
        "Lens|Industar-50 50mm f/3.5",
        "People|Anna",
    ]
    new_h = ["Roll|R002"]
    assert kw.reconcile_hierarchical(existing_h, new_h, replace_all_managed=True) == [
        "People|Anna",
        "Roll|R002",
    ]


def test_paths_are_deduped():
    ctx = _ctx(extra_keywords=["Roll|R001"])  # duplicate of the built-in
    paths = kw.build_hierarchical_paths(ctx)
    assert paths.count("Roll|R001") == 1
