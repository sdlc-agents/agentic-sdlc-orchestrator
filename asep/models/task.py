from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


TERMINAL_STATUSES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.SKIPPED,
    TaskStatus.REJECTED,
}


class Task(BaseModel):
    """A unit of work bound to exactly one agent.

    `reads`/`writes` are the task's blackboard contract; the engine refuses to
    run a task whose `reads` are not yet present, which turns a planning mistake
    into an explicit error instead of an agent hallucinating around missing input.
    """

    id: str = Field(pattern=r"^T-\d{3}(-R\d)?$")
    title: str
    agent: str
    depends_on: list[str] = []
    covers: list[str] = []
    reads: list[str] = []
    writes: list[str] = []
    risk: RiskLevel = RiskLevel.LOW
    rationale: str = ""

    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    max_attempts: int = 2
    origin: Literal["plan", "repair"] = "plan"
    error: str | None = None
    duration_ms: int = 0
    artifact_ids: list[str] = []

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def retries_left(self) -> int:
        return max(0, self.max_attempts - self.attempts)
