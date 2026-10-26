"""Journal of batch file operations (renames, later splits and imports) so
each batch can be reviewed and undone."""

from __future__ import annotations

from sqlmodel import Field, SQLModel

from filmex.models._base import new_uuid


class OperationLog(SQLModel, table=True):
    __tablename__ = "operation_log"

    id: str = Field(default_factory=new_uuid, primary_key=True)
    kind: str = Field(index=True)  # "rename" | "split" | "import"
    status: str = Field(index=True)  # "running" | "done" | "failed" | "undone"
    root: str | None = None
    started_at: str
    finished_at: str | None = None
    undone_at: str | None = None
    plan_json: str = "{}"
    result_json: str = "{}"
