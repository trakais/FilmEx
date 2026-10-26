"""Loopback guard: host and origin checks, plus the desktop session token."""

from __future__ import annotations

from fastapi.testclient import TestClient

from filmex.main import create_app
from filmex.security import TOKEN_COOKIE, host_is_loopback, origin_is_loopback

LOOPBACK = "http://127.0.0.1:8765"


def _client(token: str | None = None, base_url: str = LOOPBACK) -> TestClient:
    return TestClient(create_app(token=token), base_url=base_url)


def test_host_parsing():
    assert host_is_loopback("127.0.0.1:8765")
    assert host_is_loopback("localhost")
    assert host_is_loopback("[::1]:8765")
    assert not host_is_loopback("filmex.attacker.example:8765")
    assert not host_is_loopback("")
    assert not host_is_loopback(None)


def test_origin_parsing():
    assert origin_is_loopback("http://127.0.0.1:8765")
    assert origin_is_loopback("http://localhost:8765")
    assert not origin_is_loopback("https://127.0.0.1:8765")
    assert not origin_is_loopback("https://attacker.example")
    assert not origin_is_loopback("null")


def test_loopback_requests_pass(data_dir):
    with _client() as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/health", headers={"Origin": LOOPBACK}).status_code == 200


def test_foreign_host_is_rejected(data_dir):
    with _client(base_url="http://rebound.attacker.example") as c:
        res = c.get("/api/health")
        assert res.status_code == 403
        assert res.json()["detail"] == "Host not allowed"
        # Static assets and the page itself are not secret.
        assert c.get("/").status_code == 200


def test_foreign_origin_is_rejected(data_dir):
    with _client() as c:
        res = c.post(
            "/api/files/open?folder=/tmp&name=x.jpg", headers={"Origin": "https://attacker.example"}
        )
        assert res.status_code == 403
        assert res.json()["detail"] == "Origin not allowed"


def test_token_mode_requires_cookie(data_dir):
    with _client(token="s3cret") as c:
        res = c.get("/api/health")
        assert res.status_code == 401
        res = c.get("/api/health", cookies={TOKEN_COOKIE: "wrong"})
        assert res.status_code == 401
        res = c.get("/api/health", cookies={TOKEN_COOKIE: "s3cret"})
        assert res.status_code == 200


def test_token_is_exchanged_for_cookie_on_first_load(data_dir):
    with _client(token="s3cret") as c:
        res = c.get("/?token=s3cret", follow_redirects=False)
        assert res.status_code == 303
        assert res.headers["location"] == "/"
        assert c.cookies.get(TOKEN_COOKIE) == "s3cret"
        assert "samesite=strict" in res.headers["set-cookie"].lower()
        assert c.get("/api/health").status_code == 200


def test_wrong_token_in_url_sets_nothing(data_dir):
    with _client(token="s3cret") as c:
        res = c.get("/?token=nope", follow_redirects=False)
        assert res.status_code == 200
        assert c.cookies.get(TOKEN_COOKIE) is None
        assert c.get("/api/health").status_code == 401


def test_browser_mode_has_no_token(data_dir, monkeypatch):
    monkeypatch.delenv("FILMEX_TOKEN", raising=False)
    with _client() as c:
        assert c.get("/api/health").status_code == 200
