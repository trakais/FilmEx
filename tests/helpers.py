"""Small helpers shared by route tests."""

from __future__ import annotations


def ensure_film(client, *, brand, name, display_name, iso, short_name=None):
    """Return the film stock with this display name, creating brand/stock if needed.

    The test database is bootstrapped from the bundled starter DB, which
    already contains common brands and stocks, so creation may 409.
    """
    if client.post("/api/film-brands", json={"name": brand}).status_code not in (201, 409):
        raise AssertionError("brand create failed")
    for row in client.get("/api/film-stocks").json():
        if row["display_name"] == display_name:
            if short_name:
                row = client.patch(f"/api/film-stocks/{row['id']}", json={"short_name": short_name}).json()
            return row
    res = client.post(
        "/api/film-stocks",
        json={
            "brand": brand,
            "name": name,
            "display_name": display_name,
            "box_speed_iso": iso,
            "film_type": "color_negative",
            "process_type": "c41",
            "short_name": short_name,
        },
    )
    assert res.status_code == 201, res.text
    return res.json()
