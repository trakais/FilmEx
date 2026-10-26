"""Loopback guard for the local API.

FilmEx listens on 127.0.0.1 only, but a browser on the same machine will
happily send requests to it on behalf of any web page the user visits. This
middleware closes the two holes that matter for a loopback server:

* **DNS rebinding**: a public hostname resolving to 127.0.0.1 makes the
  browser treat the attacker's page and our API as the same origin. The
  ``Host`` header still carries the public name, so we require a loopback
  host.
* **Cross-site requests**: form posts and ``no-cors`` fetches reach us without
  a preflight. Any request carrying an ``Origin`` header must come from our
  own loopback origin.

In the desktop shell a per-launch token is added on top, carried in a
``SameSite=Strict`` cookie that the window picks up from ``/?token=...`` on
first load. Other local processes cannot obtain it, and ``SameSite=Strict``
keeps it off every cross-site request.
"""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TOKEN_COOKIE = "filmex_token"
TOKEN_QUERY_PARAM = "token"
GUARDED_PREFIX = "/api/"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def host_is_loopback(host_header: str | None) -> bool:
    """Accept ``127.0.0.1[:port]``, ``localhost[:port]`` and ``[::1][:port]``."""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    else:
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host in LOOPBACK_HOSTS


def origin_is_loopback(origin_header: str | None) -> bool:
    """Accept only ``http://<loopback host>[:port]`` origins."""
    if not origin_header:
        return False
    parts = urlsplit(origin_header.strip())
    if parts.scheme != "http":
        return False
    return (parts.hostname or "").lower() in LOOPBACK_HOSTS


class LoopbackGuard:
    """Pure ASGI middleware. See module docstring."""

    def __init__(self, app: ASGIApp, *, token: str | None = None) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(GUARDED_PREFIX):
            await self.app(scope, receive, send)
            return

        headers: dict[str, str] = {}
        for raw_key, raw_value in scope.get("headers", []):
            headers[raw_key.decode("latin-1").lower()] = raw_value.decode("latin-1")

        if not host_is_loopback(headers.get("host")):
            await _reject(send, 403, "Host not allowed")
            return
        origin = headers.get("origin")
        if origin is not None and not origin_is_loopback(origin):
            await _reject(send, 403, "Origin not allowed")
            return
        if self.token is not None and not self._cookie_matches(headers.get("cookie", "")):
            await _reject(send, 401, "Missing or invalid session token")
            return

        await self.app(scope, receive, send)

    def _cookie_matches(self, cookie_header: str) -> bool:
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == TOKEN_COOKIE and self.token is not None:
                return hmac.compare_digest(value, self.token)
        return False


async def _reject(send: Send, status: int, detail: str) -> None:
    body = ('{"detail": "' + detail + '"}').encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
