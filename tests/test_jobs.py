"""Job runner: lifecycle, replay, cancellation, exclusivity, and the apply job."""

from __future__ import annotations

import json
import subprocess
import threading
import time

import pytest

from filmex.services import jobs


@pytest.fixture
def manager():
    m = jobs.JobManager(workers=2)
    yield m
    m.shutdown()


def test_job_runs_and_replays_events(manager):
    def body(ctx):
        for i in range(3):
            ctx.emit({"type": "progress", "i": i})
        return {"total": 3}

    job = manager.submit("demo", body)
    manager.wait(job.id, timeout=5)
    assert job.status == jobs.DONE
    assert job.result == {"total": 3}
    assert job.started_at and job.finished_at

    events = list(manager.follow(job.id, heartbeat=None))
    assert [e["type"] for e in events] == ["progress", "progress", "progress", "status"]
    assert [e["seq"] for e in events] == [0, 1, 2, 3]
    assert events[-1]["status"] == jobs.DONE
    # Reconnecting replays only what was missed.
    assert [e["seq"] for e in manager.follow(job.id, from_seq=2, heartbeat=None)] == [2, 3]


def test_cancel_between_steps(manager):
    started = threading.Event()

    def body(ctx):
        out = []
        for i in range(100):
            if ctx.cancelled():
                break
            out.append(i)
            ctx.emit({"type": "progress", "i": i})
            started.set()
            time.sleep(0.01)
        return out

    job = manager.submit("demo", body)
    assert started.wait(2)
    assert manager.cancel(job.id) is True
    manager.wait(job.id, timeout=5)
    assert job.status == jobs.CANCELLED
    assert 0 < len(job.result) < 100
    assert manager.cancel(job.id) is False  # already finished


def test_failure_is_captured(manager):
    def body(ctx):
        raise ValueError("boom")

    job = manager.submit("demo", body)
    manager.wait(job.id, timeout=5)
    assert job.status == jobs.FAILED
    assert job.error == "ValueError: boom"
    last = list(manager.follow(job.id, heartbeat=None))[-1]
    assert last == {"type": "status", "status": "failed", "error": "ValueError: boom", "seq": 0}


def test_job_cancelled_exception_marks_cancelled(manager):
    def body(ctx):
        raise jobs.JobCancelled()

    job = manager.submit("demo", body)
    manager.wait(job.id, timeout=5)
    assert job.status == jobs.CANCELLED


def _handshake(mine: threading.Event, other: threading.Event):
    def body(ctx):
        mine.set()
        return other.wait(0.5)

    return body


def test_non_exclusive_jobs_overlap(manager):
    a, b = threading.Event(), threading.Event()
    ja = manager.submit("x", _handshake(a, b))
    jb = manager.submit("x", _handshake(b, a))
    manager.wait(ja.id, timeout=5)
    manager.wait(jb.id, timeout=5)
    assert ja.result is True
    assert jb.result is True


def test_exclusive_jobs_serialize(manager):
    a, b = threading.Event(), threading.Event()
    ja = manager.submit("x", _handshake(a, b), exclusive=True)
    jb = manager.submit("x", _handshake(b, a), exclusive=True)
    manager.wait(ja.id, timeout=5)
    manager.wait(jb.id, timeout=5)
    # Whichever ran first could not see the other start; the second one could.
    assert sorted([ja.result, jb.result]) == [False, True]


def test_finished_jobs_are_pruned():
    manager = jobs.JobManager(workers=1, keep_finished=2)
    try:
        for _ in range(5):
            job = manager.submit("demo", lambda ctx: None)
            manager.wait(job.id, timeout=5)
        assert len(manager.recent()) == 3  # two kept finished + the latest
        assert manager.recent()[0].id == job.id
    finally:
        manager.shutdown()


def test_job_routes(client):
    manager = jobs.get_manager()

    def body(ctx):
        ctx.emit({"type": "progress"})
        return 42

    job = manager.submit("demo", body)
    manager.wait(job.id, timeout=5)

    snapshot = client.get(f"/api/jobs/{job.id}").json()
    assert snapshot["status"] == "done"
    assert snapshot["result"] == 42
    assert snapshot["event_count"] == 2

    with client.stream("GET", f"/api/jobs/{job.id}/events") as res:
        events = [json.loads(line) for line in res.iter_lines() if line.strip()]
    assert [e["type"] for e in events] == ["progress", "status"]

    listing = client.get("/api/jobs?kind=demo").json()
    assert any(j["id"] == job.id for j in listing)
    assert "result" not in listing[0]

    assert client.get("/api/jobs/nope").status_code == 404
    assert client.post("/api/jobs/nope/cancel").status_code == 404
    assert client.post(f"/api/jobs/{job.id}/cancel").json()["cancel_requested"] is False


def _fake_ok(path, intent):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")


def test_cancelled_apply_still_writes_manifest(client, roll_factory, monkeypatch):
    from filmex.routes import workflow as wr
    from filmex.services import manifest as mf

    folder = roll_factory("2026-04-17_R777", jpegs=4)
    monkeypatch.setattr(wr, "exiftool_available", lambda: True)
    monkeypatch.setattr(wr, "read_tags", lambda path, tags: {})
    calls: list[str] = []

    def fake_apply(path, intent):
        calls.append(path.name)
        if len(calls) == 2:
            running = jobs.get_manager().recent(kind="apply")[0]
            jobs.get_manager().cancel(running.id)
        return _fake_ok(path, intent)

    monkeypatch.setattr(wr, "apply_intent", fake_apply)
    cam = client.get("/api/cameras").json()[0]
    payload = {"folder": str(folder), "camera_id": cam["id"]}
    with client.stream("POST", "/api/metadata/apply/stream", json=payload) as res:
        events = [json.loads(line) for line in res.iter_lines() if line.strip()]

    assert events[0]["type"] == "start"
    assert events[0]["job_id"]
    done = events[-1]
    assert done["type"] == "done"
    assert done["cancelled"] is True
    assert done["applied_files"] == ["scan_000.jpg", "scan_001.jpg"]

    manifest = mf.read_manifest(folder)
    assert manifest is not None
    last = manifest["apply_history"][-1]
    assert last["applied_files"] == ["scan_000.jpg", "scan_001.jpg"]
    assert last["dry_run"] is False


def test_failed_apply_reports_error_and_keeps_partial_manifest(client, roll_factory, monkeypatch):
    from filmex.routes import workflow as wr
    from filmex.services import manifest as mf

    folder = roll_factory("2026-04-17_R778", jpegs=3)
    monkeypatch.setattr(wr, "exiftool_available", lambda: True)
    monkeypatch.setattr(wr, "read_tags", lambda path, tags: {})
    calls: list[str] = []

    def fake_apply(path, intent):
        calls.append(path.name)
        if len(calls) == 2:
            raise OSError("exiftool exploded")
        return _fake_ok(path, intent)

    monkeypatch.setattr(wr, "apply_intent", fake_apply)
    cam = client.get("/api/cameras").json()[0]
    payload = {"folder": str(folder), "camera_id": cam["id"]}
    with client.stream("POST", "/api/metadata/apply/stream", json=payload) as res:
        events = [json.loads(line) for line in res.iter_lines() if line.strip()]
    assert events[-1]["type"] == "error"
    assert "exiftool exploded" in events[-1]["error"]
    assert mf.read_manifest(folder)["apply_history"][-1]["applied_files"] == ["scan_000.jpg"]

    calls.clear()
    res = client.post("/api/metadata/apply", json=payload)
    assert res.status_code == 500
    assert "exiftool exploded" in res.json()["detail"]
