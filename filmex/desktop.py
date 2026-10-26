"""Desktop entry point: FilmEx in a native window.

One process, no Terminal, no background services. The FastAPI app runs in a
daemon thread on a random loopback port, guarded by a per-launch token, and
pywebview shows it in the system webview (WKWebView on macOS, WebView2 on
Windows, WebKitGTK on Linux). Closing the window stops the server and the
process exits.

Run with ``filmex`` (console script) or ``python -m filmex.desktop``. Set
``FILMEX_SMOKE_SECONDS=N`` to open the window, log that the page came up,
and close it again after N seconds, which is how the build is checked
without a human at the keyboard.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_log = logging.getLogger("filmex.desktop")

DEFAULT_SIZE = (1240, 860)
MIN_SIZE = (1000, 700)
SERVER_START_TIMEOUT_S = 20.0
BACKGROUND = "#0b0b0b"


def free_port() -> int:
    """Ask the OS for an unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(base_url: str, timeout: float = SERVER_START_TIMEOUT_S) -> bool:
    """Poll the health endpoint until the server answers.

    Any HTTP response counts, including the guard's 401 for a request
    without the session cookie: it proves uvicorn is up and routing.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url + "/api/health", timeout=1.0):
                return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.1)
    return False


def dropped_folder(event: dict[str, Any]) -> str | None:
    """Folder to open for a pywebview document drop event.

    pywebview resolves each dropped item's absolute path into
    ``dataTransfer.files[i].pywebviewFullPath``. A dropped folder is used as
    is; a dropped file selects its parent folder, so dragging any frame of a
    roll works too.
    """
    files = (event.get("dataTransfer") or {}).get("files") or []
    for item in files:
        raw = item.get("pywebviewFullPath") if isinstance(item, dict) else None
        if not raw:
            continue
        path = Path(raw)
        return str(path if path.is_dir() else path.parent)
    return None


def open_with_default_app(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", path])
    elif sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path])


def reveal_in_file_manager(path: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    elif sys.platform.startswith("win"):
        subprocess.Popen(["explorer", f"/select,{path}"])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


class DesktopApi:
    """Methods the page can call as ``window.pywebview.api.<name>()``."""

    def __init__(self) -> None:
        self.window: Any = None

    def ping(self) -> str:
        return "pong"

    def pick_folder(self, start_dir: str | None = None) -> str | None:
        """Native folder dialog; returns the chosen path or None on cancel."""
        if self.window is None:
            return None
        import webview

        result = self.window.create_file_dialog(
            webview.FileDialog.FOLDER, directory=start_dir or ""
        )
        if not result:
            return None
        first = result[0] if isinstance(result, (list, tuple)) else result
        return str(first) or None

    def open_path(self, path: str) -> bool:
        open_with_default_app(path)
        return True

    def reveal_path(self, path: str) -> bool:
        reveal_in_file_manager(path)
        return True


class Shell:
    def __init__(self, *, smoke_seconds: float | None = None) -> None:
        self.smoke_seconds = smoke_seconds
        self.window: Any = None
        self._server: Any = None
        self._server_thread: threading.Thread | None = None
        self._geometry_timer: threading.Timer | None = None

    # ---------- lifecycle ----------

    def run(self) -> int:
        from filmex.security import new_token

        token = new_token()
        # The app reads the token at import time, so set it first.
        os.environ["FILMEX_TOKEN"] = token

        import uvicorn
        import webview
        from sqlmodel import Session

        from filmex.config import data_dir, ensure_data_dir
        from filmex.db import get_engine, init_db
        from filmex.main import app
        from filmex.services import settings as settings_service

        ensure_data_dir()
        init_db()
        with Session(get_engine()) as session:
            settings = settings_service.get_all(session)

        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", access_log=False
        )
        self._server = uvicorn.Server(config)
        self._server_thread = threading.Thread(
            target=self._server.run, name="filmex-server", daemon=True
        )
        self._server_thread.start()
        if not wait_for_server(base_url):
            _log.error("server did not come up on %s", base_url)
            self._stop_server()
            return 1
        _log.info("server listening on %s", base_url)

        geometry = settings.get("window_geometry") or {}
        api = DesktopApi()
        self.window = webview.create_window(
            "FilmEx",
            f"{base_url}/?token={token}",
            js_api=api,
            width=int(geometry.get("width") or DEFAULT_SIZE[0]),
            height=int(geometry.get("height") or DEFAULT_SIZE[1]),
            x=geometry.get("x"),
            y=geometry.get("y"),
            min_size=MIN_SIZE,
            background_color=BACKGROUND,
            text_select=True,
        )
        api.window = self.window
        self.window.events.loaded += self._on_loaded
        self.window.events.closing += self._on_closing
        # `destroy()` and force quits skip `closing`, so also persist geometry
        # shortly after every resize or move.
        self.window.events.resized += self._on_geometry_changed
        self.window.events.moved += self._on_geometry_changed

        if self.smoke_seconds is not None:
            threading.Timer(self.smoke_seconds, self._smoke_close).start()

        storage = data_dir() / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        webview.start(
            private_mode=False,
            storage_path=str(storage),
            debug=bool(os.environ.get("FILMEX_DEBUG")),
        )
        self._stop_server()
        _log.info("window closed, server stopped")
        return 0

    def _stop_server(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._server_thread is not None:
            self._server_thread.join(timeout=10)

    # ---------- window events ----------

    def _on_loaded(self) -> None:
        window = self.window
        url = window.get_current_url() or ""
        if "token=" in url:
            # First load exchanges the token for a cookie and redirects to /;
            # the redirect target fires `loaded` again with the real document.
            return
        window.dom.document.events.drop += self._on_drop
        _log.info("page loaded: %s", url)
        if self.smoke_seconds is not None:
            self._smoke_report()

    def _on_drop(self, event: dict[str, Any]) -> None:
        folder = dropped_folder(event)
        if not folder:
            return
        self.window.evaluate_js(
            "window.filmexDesktop && window.filmexDesktop.onDrop(%s)" % json.dumps([folder])
        )

    def _on_geometry_changed(self, *_args: Any) -> None:
        if self._geometry_timer is not None:
            self._geometry_timer.cancel()
        self._geometry_timer = threading.Timer(0.8, self._save_geometry)
        self._geometry_timer.daemon = True
        self._geometry_timer.start()

    def _on_closing(self) -> None:
        """Persist window geometry. Returning None keeps the close going."""
        if self._geometry_timer is not None:
            self._geometry_timer.cancel()
        self._save_geometry()

    def _save_geometry(self) -> None:
        window = self.window
        try:
            geometry = {
                "width": int(window.width),
                "height": int(window.height),
                "x": int(window.x),
                "y": int(window.y),
            }
        except Exception as exc:  # noqa: BLE001 - geometry is a convenience only
            _log.warning("could not read window geometry: %s", exc)
            return
        try:
            from sqlmodel import Session

            from filmex.db import get_engine
            from filmex.services import settings as settings_service

            with Session(get_engine()) as session:
                settings_service.update(session, {"window_geometry": geometry})
        except Exception as exc:  # noqa: BLE001
            _log.warning("could not save window geometry: %s", exc)

    # ---------- smoke mode ----------

    def _smoke_report(self) -> None:
        """Log whether the page initialised through the authenticated API."""
        try:
            ready = ""
            for _ in range(50):
                ready = self.window.evaluate_js("document.body.dataset.ready || ''")
                if ready:
                    break
                time.sleep(0.1)
            title = self.window.evaluate_js("document.title")
            bridge = self.window.evaluate_js("typeof window.filmexDesktop")
            _log.info("smoke: title=%r ready=%r bridge=%r", title, ready, bridge)
            # Exercise the geometry path, which `destroy()` would otherwise skip.
            self.window.resize(DEFAULT_SIZE[0] - 40, DEFAULT_SIZE[1] - 40)
        except Exception as exc:  # noqa: BLE001
            _log.warning("smoke report failed: %s", exc)

    def _smoke_close(self) -> None:
        _log.info("smoke: closing window after %ss", self.smoke_seconds)
        try:
            self.window.destroy()
        except Exception as exc:  # noqa: BLE001
            _log.warning("smoke close failed: %s", exc)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    smoke = os.environ.get("FILMEX_SMOKE_SECONDS")
    shell = Shell(smoke_seconds=float(smoke) if smoke else None)
    return shell.run()


if __name__ == "__main__":
    sys.exit(main())
