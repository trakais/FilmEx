"""Job endpoints: snapshots, event streams, cancellation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from filmex.services import jobs

router = APIRouter()


def ndjson(events: Iterator[dict[str, Any]]) -> Iterator[str]:
    for event in events:
        yield json.dumps(event) + "\n"


@router.get("/api/jobs")
def list_jobs(limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
    manager = jobs.get_manager()
    return [job.snapshot(include_result=False) for job in manager.recent(limit=limit, kind=kind)]


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = jobs.get_manager().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return job.snapshot()


@router.get("/api/jobs/{job_id}/events")
def job_events(job_id: str, from_seq: int = 0) -> StreamingResponse:
    manager = jobs.get_manager()
    if manager.get(job_id) is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    return StreamingResponse(
        ndjson(manager.follow(job_id, from_seq=from_seq)),
        media_type="application/x-ndjson",
    )


@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    manager = jobs.get_manager()
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    requested = manager.cancel(job_id)
    return {"ok": True, "cancel_requested": requested, "status": job.status}
