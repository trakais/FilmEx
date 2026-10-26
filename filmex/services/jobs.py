"""In-process job runner for long operations.

Applies, preview builds, library scans, renames, splits, imports and undos
run here as threads inside the app process, never as separate processes. A
job keeps an ordered event log so a client can follow it live, reconnect
after a reload and replay what it missed, or poll a snapshot. Jobs flagged
``exclusive`` are the ones that mutate the user's files; they are
serialized so two never touch a folder at the same time.

The event protocol is owned here, so every job speaks the same one:

- ``start``: emitted on submission, ``{"type": "start", "job_id", "kind",
  ...}`` plus whatever the caller passed as ``start`` (totals, mode).
- any number of body events, usually ``progress``, via ``JobContext.emit``.
- exactly one terminal event, emitted by the manager when the body ends:
  ``done`` carries the body's return value (a dict is spread into the
  event, anything else goes under ``result``); ``error`` carries the
  message when the body raised. Both carry ``job_status`` (``done``,
  ``cancelled`` or ``failed``). A body that notices a cancel and returns
  normally still ends with ``done``.
- ``heartbeat`` while following an idle job; readers ignore it.

Bodies never emit ``start``, ``done`` or ``error`` themselves.

Nothing here knows about HTTP. ``routes/jobs.py`` streams the log.
"""

from __future__ import annotations

import logging
import threading
import traceback
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_log = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL = frozenset({DONE, FAILED, CANCELLED})

START_EVENT = "start"
DONE_EVENT = "done"
ERROR_EVENT = "error"
HEARTBEAT_EVENT = "heartbeat"
_RESERVED_EVENTS = frozenset({START_EVENT, DONE_EVENT, ERROR_EVENT, HEARTBEAT_EVENT})


class JobCancelled(Exception):
    """Raised by a job body to stop early after observing a cancel request."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Job:
    id: str
    kind: str
    exclusive: bool
    status: str = QUEUED
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    result: Any = None
    error: str | None = None
    cancel_requested: threading.Event = field(default_factory=threading.Event, repr=False)
    cond: threading.Condition = field(default_factory=threading.Condition, repr=False)

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL

    def snapshot(self, *, include_result: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "exclusive": self.exclusive,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "event_count": len(self.events),
            "error": self.error,
        }
        if include_result:
            data["result"] = self.result
        return data


class JobContext:
    """What a job body gets: a way to report progress and to notice cancels."""

    def __init__(self, job: Job) -> None:
        self._job = job

    @property
    def id(self) -> str:
        return self._job.id

    def emit(self, event: dict[str, Any]) -> None:
        if event.get("type") in _RESERVED_EVENTS:
            raise ValueError(f"job bodies may not emit {event.get('type')!r} events; return a result instead")
        _append(self._job, event)

    def cancelled(self) -> bool:
        return self._job.cancel_requested.is_set()

    def check_cancelled(self) -> None:
        if self.cancelled():
            raise JobCancelled()


JobBody = Callable[[JobContext], Any]


def _append(job: Job, event: dict[str, Any]) -> None:
    with job.cond:
        job.events.append({**event, "seq": len(job.events)})
        job.cond.notify_all()


class JobManager:
    def __init__(self, *, workers: int = 2, keep_finished: int = 200) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="filmex-job")
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._exclusive = threading.Lock()
        self._keep_finished = keep_finished

    # ---------- submission ----------

    def submit(
        self,
        kind: str,
        body: JobBody,
        *,
        exclusive: bool = False,
        start: dict[str, Any] | None = None,
    ) -> Job:
        """Queue ``body``. ``start`` adds fields (totals, mode) to the job's
        ``start`` event so a reader knows what to expect."""
        job = Job(id=uuid.uuid4().hex, kind=kind, exclusive=exclusive)
        _append(job, {**(start or {}), "type": START_EVENT, "job_id": job.id, "kind": kind})
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()
        self._executor.submit(self._run, job, body)
        return job

    def _run(self, job: Job, body: JobBody) -> None:
        if job.exclusive:
            with self._exclusive:
                self._execute(job, body)
        else:
            self._execute(job, body)

    def _execute(self, job: Job, body: JobBody) -> None:
        if job.cancel_requested.is_set():
            self._finish(job, CANCELLED)
            return
        with job.cond:
            job.status = RUNNING
            job.started_at = _now()
            job.cond.notify_all()
        try:
            result = body(JobContext(job))
        except JobCancelled:
            self._finish(job, CANCELLED)
        except Exception as exc:  # noqa: BLE001 - a job must never kill the worker
            _log.error("job %s (%s) failed:\n%s", job.id, job.kind, traceback.format_exc())
            self._finish(job, FAILED, error=f"{type(exc).__name__}: {exc}")
        else:
            status = CANCELLED if job.cancel_requested.is_set() else DONE
            self._finish(job, status, result=result)

    def _finish(self, job: Job, status: str, *, result: Any = None, error: str | None = None) -> None:
        if status == FAILED or (status == CANCELLED and result is None):
            terminal = {"type": ERROR_EVENT, "error": error or "cancelled"}
        elif isinstance(result, dict):
            terminal = {**result, "type": DONE_EVENT}
        else:
            terminal = {"type": DONE_EVENT, "result": result}
        terminal["job_status"] = status
        with job.cond:
            job.status = status
            job.result = result
            job.error = error
            job.finished_at = _now()
            job.events.append({**terminal, "seq": len(job.events)})
            job.cond.notify_all()

    # ---------- queries ----------

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, *, limit: int = 50, kind: str | None = None) -> list[Job]:
        with self._lock:
            ids = list(reversed(self._order))
            jobs = [self._jobs[i] for i in ids]
        if kind is not None:
            jobs = [j for j in jobs if j.kind == kind]
        return jobs[:limit]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.finished:
            return False
        job.cancel_requested.set()
        return True

    def wait(self, job_id: str, timeout: float | None = None) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        with job.cond:
            if not job.cond.wait_for(lambda: job.finished, timeout=timeout):
                raise TimeoutError(f"job {job_id} did not finish within {timeout}s")
        return job

    def follow(
        self,
        job_id: str,
        *,
        from_seq: int = 0,
        heartbeat: float | None = 15.0,
    ) -> Iterator[dict[str, Any]]:
        """Yield events from ``from_seq`` on, blocking until the job finishes.

        Past events are replayed first, so a client that reconnects after a
        reload sees the whole history. A heartbeat event is yielded when
        nothing happens for ``heartbeat`` seconds so idle streams stay alive.
        """
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        index = max(0, from_seq)
        while True:
            with job.cond:
                if index >= len(job.events) and not job.finished:
                    job.cond.wait(timeout=heartbeat)
                batch = job.events[index:]
                index = len(job.events)
                finished = job.finished
            if not batch and not finished and heartbeat is not None:
                yield {"type": HEARTBEAT_EVENT}
                continue
            yield from batch
            if finished and index >= len(job.events):
                return

    # ---------- lifecycle ----------

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _prune_locked(self) -> None:
        finished = [i for i in self._order if self._jobs[i].finished]
        excess = len(finished) - self._keep_finished
        for job_id in finished[: max(0, excess)]:
            self._order.remove(job_id)
            del self._jobs[job_id]


_manager: JobManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> JobManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = JobManager()
        return _manager


def reset_manager() -> None:
    """Drop the process-wide manager. Tests use this between runs."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown()
        _manager = None
