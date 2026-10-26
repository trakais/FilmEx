"""Folder naming: slugs, template rendering, short names, and the place field."""

from __future__ import annotations

import unicodedata

import pytest

from filmex.services import naming


def test_slug_keeps_unicode_letters_and_drops_illegal_chars():
    assert naming.slug("Jāņos") == "Jāņos"
    assert naming.slug(unicodedata.normalize("NFD", "Jāņos")) == "Jāņos"
    assert naming.slug("Nikon F3") == "Nikon-F3"
    assert naming.slug("  Kodak   Gold 200 ") == "Kodak-Gold-200"
    assert naming.slug('a/b\\c:d*e?f"g<h>i|j') == "abcdefghij"
    assert naming.slug("--x--") == "x"
    assert naming.slug(None) == ""
    assert naming.slug("...") == ""


def test_short_name_prefers_explicit_value():
    assert naming.short_name_for("Nikon F3", "F3") == "F3"
    assert naming.short_name_for("Nikon F3", "  ") == "Nikon-F3"
    assert naming.short_name_for("Kodak Gold 200", None) == "Kodak-Gold-200"


def test_render_default_template_matches_user_style():
    ctx = naming.NameContext(date="2026-05-14", camera="Fed2", film="Gold")
    assert naming.render(naming.DEFAULT_TEMPLATE, ctx) == "2026-05-14-Fed2-Gold"


def test_render_place_suffix_and_inline_place():
    ctx = naming.NameContext(date="2026-06-20", camera="Fujica", film="ColorPlus", place="Jāņos")
    assert naming.render("{date}-{camera}-{film}", ctx) == "2026-06-20-Fujica-ColorPlus-Jāņos"
    assert naming.render("{date}-{camera}-{film}", ctx, place_suffix=False) == "2026-06-20-Fujica-ColorPlus"
    assert naming.render("{year}-{month}_{place}_{camera}", ctx) == "2026-06_Jāņos_Fujica"
    no_place = naming.NameContext(date="2026-06-20", camera="Fujica", film="ColorPlus")
    assert naming.render("{year}-{month}_{place}_{camera}", no_place) == "2026-06_Fujica"
    assert naming.render("{date}-{camera}-{film}", no_place) == "2026-06-20-Fujica-ColorPlus"


def test_render_roll_number_and_old_style_template():
    ctx = naming.NameContext(date="2024-08-03", camera="F3", film="Gold", place="Laivas", roll=2)
    assert naming.render("{year}-{month}_R{roll}_{camera}_{film}_{place}", ctx) == "2024-08_R02_F3_Gold_Laivas"
    assert naming.render("{year}-{month}_R{roll}_{camera}_{film}", ctx, roll_pad=1) == "2024-08_R2_F3_Gold-Laivas"


def test_render_missing_values_and_bad_templates():
    with pytest.raises(naming.NamingError, match="missing value for {film}"):
        naming.render("{date}-{camera}-{film}", naming.NameContext(date="2026-01-01", camera="X"))
    with pytest.raises(naming.NamingError, match="unknown token"):
        naming.validate_template("{date}-{lab}")
    with pytest.raises(naming.NamingError, match="no tokens"):
        naming.validate_template("rolls")
    with pytest.raises(naming.NamingError, match="empty"):
        naming.validate_template("  ")
    with pytest.raises(naming.NamingError, match="YYYY-MM-DD"):
        naming.render("{date}", naming.NameContext(date="20260101"))
    with pytest.raises(naming.NamingError, match="illegal"):
        naming.validate_template("{date}/{camera}")


def test_same_name_ignores_normalization_form():
    nfc = "2026-06-C41-Fujica-Jāņos"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfc != nfd
    assert naming.same_name(nfc, nfd)
    assert naming.tokens_in("{date}-{camera}") == ["date", "camera"]


def test_short_name_round_trips_through_the_api(client):
    cam = client.post(
        "/api/cameras",
        json={"make": "Nikon", "model": "F3", "display_name": "Nikon F3", "mount": "Nikon F", "short_name": "F3"},
    ).json()
    assert cam["short_name"] == "F3"
    listed = next(c for c in client.get("/api/cameras").json() if c["id"] == cam["id"])
    assert listed["short_name"] == "F3"
    patched = client.patch(f"/api/cameras/{cam['id']}", json={"short_name": "F3HP"}).json()
    assert patched["short_name"] == "F3HP"

    brand = client.post("/api/film-brands", json={"name": "Kodak"}).json()
    assert brand["name"] == "Kodak"
    film = client.post(
        "/api/film-stocks",
        json={"brand": "Kodak", "name": "Gold 200", "display_name": "Kodak Gold 200", "box_speed_iso": 200,
              "film_type": "color_negative", "process_type": "c41", "short_name": "Gold"},
    ).json()
    assert film["short_name"] == "Gold"


def test_place_is_stored_in_the_manifest(client, roll_factory, monkeypatch):
    import subprocess

    from filmex.routes import workflow as wr
    from filmex.services import manifest as mf

    folder = roll_factory("2026-06-20-Fujica-ColorPlus", jpegs=1)
    monkeypatch.setattr(wr, "exiftool_available", lambda: True)
    monkeypatch.setattr(wr, "read_tags", lambda path, tags: {})
    monkeypatch.setattr(wr, "apply_intent", lambda path, intent: subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b""))
    cam = client.get("/api/cameras").json()[0]
    res = client.post("/api/metadata/apply", json={"folder": str(folder), "camera_id": cam["id"], "place": "  Jāņos "})
    assert res.status_code == 200, res.text
    manifest = mf.read_manifest(folder)
    assert manifest["place"] == "Jāņos"
    assert client.get("/api/manifest", params={"folder": str(folder)}).json()["manifest"]["place"] == "Jāņos"
