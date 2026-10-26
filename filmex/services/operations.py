"""Operation log: start, finish, undo bookkeeping for batch file operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from filmex.models import OperationLog

RUNNING = "running"
DONE = "done"
FAILED = "failed"
UNDONE = "undone"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def start(session: Session, kind: str, plan: dict[str, Any], *, root: str | None = None) -> OperationLog:
    row = OperationLog(kind=kind, status=RUNNING, root=root, started_at=_now(), plan_json=json.dumps(plan))
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def finish(session: Session, op_id: str, status: str, result: dict[str, Any]) -> OperationLog:
    row = session.get(OperationLog, op_id)
    if row is None:
        raise KeyError(op_id)
    row.status = status
    row.finished_at = _now()
    row.result_json = json.dumps(result)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def mark_undone(session: Session, op_id: str, result: dict[str, Any] | None = None) -> OperationLog:
    row = session.get(OperationLog, op_id)
    if row is None:
        raise KeyError(op_id)
    row.status = UNDONE
    row.undone_at = _now()
    if result is not None:
        merged = json.loads(row.result_json or "{}")
        merged["undo"] = result
        row.result_json = json.dumps(merged)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def get(session: Session, op_id: str) -> OperationLog | None:
    return session.get(OperationLog, op_id)


def to_dict(row: OperationLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "root": row.root,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "undone_at": row.undone_at,
        "plan": json.loads(row.plan_json or "{}"),
        "result": json.loads(row.result_json or "{}"),
    }


def recent(session: Session, *, kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    stmt = select(OperationLog)
    if kind:
        stmt = stmt.where(OperationLog.kind == kind)
    rows = session.exec(stmt).all()
    rows.sort(key=lambda r: r.started_at, reverse=True)
    return [to_dict(r) for r in rows[:limit]]
