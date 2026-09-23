from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .artifact import Artifact
from .task import Task, TaskStatus


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_CLARIFICATION = "needs_clarification"
    REJECTED_BY_HUMAN = "rejected_by_human"


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    TASK_SCHEDULED = "task_scheduled"
    TASK_STARTED = "task_started"
    TASK_SUCCEEDED = "task_succeeded"
    TASK_FAILED = "task_failed"
    TASK_RETRIED = "task_retried"
    TASK_SKIPPED = "task_skipped"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    VALIDATION_FAILED = "validation_failed"
    VALIDATION_PASSED = "validation_passed"
    REPAIR_SCHEDULED = "repair_scheduled"
    GRAPH_MUTATED = "graph_mutated"
    ESCALATED = "escalated"
    CLARIFICATION_REQUESTED = "clarification_requested"


class Event(BaseModel):
    seq: int
    ts: float = Field(default_factory=time.time)
    type: EventType
    task_id: str | None = None
    agent: str | None = None
    message: str = ""
    data: dict[str, Any] = {}


class ApprovalDecision(BaseModel):
    task_id: str
    approved: bool
    approver: str
    reason: str = ""
    ts: float = Field(default_factory=time.time)


class Blackboard(BaseModel):
    """Shared, typed working memory.

    Agents never call each other. They read and write keys here, and the engine
    enforces each task's declared contract, so coordination is inspectable state
    rather than an implicit call chain.
    """

    data: dict[str, Any] = {}

    def has(self, key: str) -> bool:
        return key in self.data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def require(self, key: str) -> Any:
        if key not in self.data:
            raise KeyError(f"blackboard key not available: {key}")
        return self.data[key]

    def put(self, key: str, value: Any) -> None:
        self.data[key] = value

    @property
    def keys(self) -> list[str]:
        return sorted(self.data)


class RunState(BaseModel):
    run_id: str
    requirement: str
    scenario: str
    mode: str
    status: RunStatus = RunStatus.RUNNING
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None

    tasks: dict[str, Task] = {}
    artifacts: dict[str, Artifact] = {}
    events: list[Event] = []
    approvals: list[ApprovalDecision] = []
    blackboard: Blackboard = Blackboard()

    def emit(
        self,
        type: EventType,
        message: str = "",
        task_id: str | None = None,
        agent: str | None = None,
        **data: Any,
    ) -> Event:
        event = Event(
            seq=len(self.events) + 1,
            type=type,
            task_id=task_id,
            agent=agent,
            message=message,
            data=data,
        )
        self.events.append(event)
        return event

    def add_artifact(self, artifact: Artifact) -> None:
        self.artifacts[artifact.id] = artifact
        task = self.tasks.get(artifact.produced_by)
        if task and artifact.id not in task.artifact_ids:
            task.artifact_ids.append(artifact.id)

    @property
    def duration_ms(self) -> int:
        end = self.finished_at or time.time()
        return int((end - self.started_at) * 1000)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for task in self.tasks.values():
            out[task.status.value] = out.get(task.status.value, 0) + 1
        return out

    @property
    def succeeded_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status == TaskStatus.SUCCEEDED]
