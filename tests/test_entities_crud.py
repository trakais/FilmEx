"""Entity CRUD endpoints."""

from __future__ import annotations


def test_list_cameras_excludes_archived_by_default(client):
    cameras = client.get("/api/cameras").json()
    assert cameras, "seed data should include cameras"
    assert all(not c["archived"] for c in cameras)


def test_create_camera(client):
    res = client.post(
        "/api/cameras",
        json={
            "make": "Pentax",
            "model": "K1000",
            "display_name": "Pentax K1000",
            "mount": "K",
            "camera_type": "slr",
        },
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["make"] == "Pentax"
    assert body["id"]

    listed_ids = {c["id"] for c in client.get("/api/cameras").json()}
    assert body["id"] in listed_ids


def test_entity_list_roundtrips_notes_and_aliases(client):
    created = client.post(
        "/api/cameras",
        json={
            "make": "Canon",
            "model": "P",
            "display_name": "Canon P",
            "notes": "Rangefinder body",
            "aliases": ["Canon Populaire"],
        },
    )
    assert created.status_code == 201, created.text
    camera_id = created.json()["id"]

    listed = client.get("/api/cameras").json()
    row = next(c for c in listed if c["id"] == camera_id)
    assert row["notes"] == "Rangefinder body"
    assert row["aliases"] == ["Canon Populaire"]


def test_create_camera_rejects_unknown_fixed_lens(client):
    res = client.post(
        "/api/cameras",
        json={
            "make": "X",
            "model": "Y",
            "display_name": "X Y",
            "fixed_lens_id": "definitely-not-a-real-id",
        },
    )
    assert res.status_code == 400


def test_camera_mount_is_trimmed(client):
    res = client.post(
        "/api/cameras",
        json={
            "make": "Leica",
            "model": "M4",
            "display_name": "Leica M4",
            "mount": "  Leica M  ",
        },
    )
    assert res.status_code == 201, res.text
    assert res.json()["mount"] == "Leica M"


def test_patch_camera_partial(client):
    first = client.get("/api/cameras").json()[0]
    res = client.patch(f"/api/cameras/{first['id']}", json={"favorite": not first["favorite"]})
    assert res.status_code == 200
    assert res.json()["favorite"] != first["favorite"]


def test_archive_and_unarchive_camera(client):
    first = client.get("/api/cameras").json()[0]

    archived = client.post(f"/api/cameras/{first['id']}/archive").json()
    assert archived["archived"] is True

    # Default list hides it
    ids = {c["id"] for c in client.get("/api/cameras").json()}
    assert first["id"] not in ids

    # include_archived surfaces it
    ids_all = {c["id"] for c in client.get("/api/cameras?include_archived=true").json()}
    assert first["id"] in ids_all

    # Unarchive brings it back
    unarchived = client.post(f"/api/cameras/{first['id']}/unarchive").json()
    assert unarchived["archived"] is False


def test_404_on_missing_entity(client):
    assert client.patch("/api/cameras/nope", json={}).status_code == 404
    assert client.post("/api/cameras/nope/archive").status_code == 404


def test_lens_mount_filter_by_camera(client):
    cameras = client.get("/api/cameras").json()
    # Pick a camera with a non-fixed, non-null mount
    candidate = next(
        (
            c
            for c in cameras
            if c["mount"] and c["mount"].lower() != "fixed-lens" and not c["fixed_lens_id"]
        ),
        None,
    )
    if candidate is None:
        # Not all seed sets have one; skip quietly.
        import pytest

        pytest.skip("No camera with a mount in seed data")

    lenses = client.get(f"/api/lenses?camera_id={candidate['id']}").json()
    # Every returned lens should either share the mount or have mount==null.
    for lens in lenses:
        assert lens["mount"] is None or lens["mount"] == candidate["mount"]


def test_fixed_lens_camera_returns_only_that_lens(client):
    cameras = client.get("/api/cameras").json()
    fixed = next((c for c in cameras if c["fixed_lens_id"]), None)
    if fixed is None:
        import pytest

        pytest.skip("No fixed-lens camera in seed data")
    lenses = client.get(f"/api/lenses?camera_id={fixed['id']}").json()
    assert len(lenses) == 1
    assert lenses[0]["id"] == fixed["fixed_lens_id"]


def test_list_camera_mounts_returns_unique_sorted_mounts(client):
    client.post(
        "/api/cameras",
        json={
            "make": "Canon",
            "model": "AE-1",
            "display_name": "Canon AE-1",
            "mount": "FD",
        },
    )
    client.post(
        "/api/cameras",
        json={
            "make": "Canon",
            "model": "A-1",
            "display_name": "Canon A-1",
            "mount": " FD ",
        },
    )
    mounts = client.get("/api/camera-mounts").json()
    assert "FD" in mounts
    assert mounts.count("FD") == 1


def test_provider_kind_filter(client):
    dev = client.get("/api/providers?kind=development").json()
    scan = client.get("/api/providers?kind=scan").json()
    # Default DB ships the generic fallbacks; specific labs are user-added.
    assert {p["name"] for p in dev} >= {"Home"}
    assert {p["name"] for p in scan} >= {"Self"}


def test_create_provider_requires_at_least_one_kind(client):
    res = client.post("/api/providers", json={"name": "Orphan", "kinds": []})
    assert res.status_code == 400


def test_create_provider_rejects_unknown_kind(client):
    res = client.post("/api/providers", json={"name": "X", "kinds": ["pony"]})
    assert res.status_code == 400


def test_film_brand_list_ships_with_seed_brands(client):
    brands = client.get("/api/film-brands").json()
    names = {b["name"] for b in brands}
    # Seed data in default_data.sqlite covers these; if you rebuild the seed,
    # keep the set a superset of this.
    assert {"Kodak", "Ilford", "Fujifilm"} <= names


def test_film_brand_create_normalizes_hex(client):
    created = client.post(
        "/api/film-brands",
        json={
            "name": "Revolog",
            "color_primary": "#A1B2C3",
            "color_secondary": "abc",  # shorthand, no leading #
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["color_primary"] == "#a1b2c3"
    assert body["color_secondary"] == "#aabbcc"


def test_film_brand_rejects_garbage_hex(client):
    res = client.post(
        "/api/film-brands",
        json={"name": "BadColor", "color_primary": "rainbow"},
    )
    assert res.status_code == 422


def test_film_brand_create_rejects_duplicate_name(client):
    client.post("/api/film-brands", json={"name": "Adox"})
    clash = client.post("/api/film-brands", json={"name": "adox"})
    assert clash.status_code == 409


def test_film_brand_rename_cascades_to_film_stocks(client):
    brand = client.post("/api/film-brands", json={"name": "Rera"}).json()
    stock = client.post(
        "/api/film-stocks",
        json={
            "brand": "Rera",
            "name": "Pan 100-400",
            "display_name": "Rera Pan 100-400",
            "box_speed_iso": 400,
            "film_type": "bw_negative",
            "process_type": "bw",
        },
    ).json()
    renamed = client.patch(
        f"/api/film-brands/{brand['id']}",
        json={"name": "Rera Pan"},
    )
    assert renamed.status_code == 200, renamed.text

    # Film stock now references the new brand name.
    films = client.get("/api/film-stocks").json()
    ours = next(f for f in films if f["id"] == stock["id"])
    assert ours["brand"] == "Rera Pan"


def test_film_stock_rejects_unknown_brand(client):
    res = client.post(
        "/api/film-stocks",
        json={
            "brand": "NotARealBrand",
            "name": "Bogus",
            "display_name": "NotARealBrand Bogus 400",
            "box_speed_iso": 400,
            "film_type": "color_negative",
            "process_type": "c41",
        },
    )
    assert res.status_code == 400
    assert "Unknown film brand" in res.text


def test_film_stock_crud_roundtrip(client):
    created = client.post(
        "/api/film-stocks",
        json={
            "brand": "Kodak",
            "name": "Ektar",
            "display_name": "Kodak Ektar 100",
            "box_speed_iso": 100,
            "film_type": "color_negative",
            "process_type": "c41",
        },
    )
    assert created.status_code == 201
    fid = created.json()["id"]

    updated = client.patch(f"/api/film-stocks/{fid}", json={"favorite": True}).json()
    assert updated["favorite"] is True

    archived = client.post(f"/api/film-stocks/{fid}/archive").json()
    assert archived["archived"] is True


def test_lens_rejects_non_positive_aperture(client):
    for bad in (0, -1.4):
        res = client.post(
            "/api/lenses",
            json={
                "make": "X",
                "model": "Y",
                "display_name": "X Y",
                "lens_model_write_value": "X Y",
                "mount": client.get("/api/camera-mounts").json()[0],
                "max_aperture": bad,
            },
        )
        assert res.status_code == 422, f"aperture={bad} should have been rejected"


def test_create_lens_requires_existing_camera_mount(client):
    res = client.post(
        "/api/lenses",
        json={
            "make": "Canon",
            "model": "50",
            "display_name": "Canon 50mm f/1.4",
            "lens_model_write_value": "Canon 50mm f/1.4",
            "mount": "FD",
        },
    )
    assert res.status_code == 400
    assert "existing camera mount" in res.text

    camera = client.post(
        "/api/cameras",
        json={
            "make": "Canon",
            "model": "AE-1",
            "display_name": "Canon AE-1",
            "mount": "FD",
        },
    )
    assert camera.status_code == 201, camera.text

    ok = client.post(
        "/api/lenses",
        json={
            "make": "Canon",
            "model": "50",
            "display_name": "Canon 50mm f/1.4",
            "lens_model_write_value": "Canon 50mm f/1.4",
            "mount": " FD ",
        },
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["mount"] == "FD"


def test_update_lens_rejects_unknown_mount_change(client):
    first = client.get("/api/lenses").json()[0]
    res = client.patch(f"/api/lenses/{first['id']}", json={"mount": "Imaginary Mount"})
    assert res.status_code == 400
    assert "existing camera mount" in res.text


def test_methods_crud(client):
    for path in ("/api/development-methods", "/api/scan-methods"):
        res = client.post(path, json={"name": f"Test method {path}"})
        assert res.status_code == 201
        mid = res.json()["id"]
        assert client.patch(f"{path}/{mid}", json={"favorite": True}).json()["favorite"] is True
        assert client.post(f"{path}/{mid}/archive").json()["archived"] is True
        assert client.post(f"{path}/{mid}/unarchive").json()["archived"] is False
