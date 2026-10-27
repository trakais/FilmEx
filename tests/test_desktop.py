"""Desktop shell helpers that run without a display."""

from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from filmex import desktop


def test_free_port_is_bindable():
    port = desktop.free_port()
    assert 1024 < port < 65536
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


def test_dropped_folder_prefers_folder_then_parent(tmp_path):
    folder = tmp_path / "roll"
    folder.mkdir()
    frame = folder / "DSC00001.ARW"
    frame.write_bytes(b"x")

    def event(path):
        return {"dataTransfer": {"files": [{"name": "n", "pywebviewFullPath": path}]}}

    assert desktop.dropped_folder(event(str(folder))) == str(folder)
    assert desktop.dropped_folder(event(str(frame))) == str(folder)
    assert desktop.dropped_folder({"dataTransfer": {"files": [{"name": "n"}]}}) is None
    assert desktop.dropped_folder({}) is None


class _StubWindow:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def create_file_dialog(self, dialog_type, directory=""):
        self.calls.append((int(dialog_type), directory))
        return self.result


def test_pick_folder_uses_native_folder_dialog():
    api = desktop.DesktopApi()
    assert api.pick_folder() is None  # no window yet

    api.window = _StubWindow(["/Volumes/homes/x/Photos"])
    assert api.pick_folder("/Volumes") == "/Volumes/homes/x/Photos"
    assert api.window.calls == [(20, "/Volumes")]  # 20 == FileDialog.FOLDER

    api.window = _StubWindow(None)
    assert api.pick_folder() is None
    api.window = _StubWindow(())
    assert api.pick_folder() is None


class _Unauthorized(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server naming
        self.send_response(401)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_wait_for_server_accepts_guarded_401():
    server = HTTPServer(("127.0.0.1", 0), _Unauthorized)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert desktop.wait_for_server(f"http://127.0.0.1:{server.server_port}", timeout=5)
    finally:
        server.shutdown()


def test_wait_for_server_times_out_on_closed_port():
    port = desktop.free_port()
    assert desktop.wait_for_server(f"http://127.0.0.1:{port}", timeout=0.5) is False



def test_match_theme_without_a_native_window_is_a_no_op():
    api = desktop.DesktopApi()
    assert api.match_theme("dark") is False

    class _NoNative:
        native = None

    api.window = _NoNative()
    assert api.match_theme("paper") is False
    assert desktop.match_title_bar(None, "dark") is False
