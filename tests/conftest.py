from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from asep.models import RiskLevel, RunState, Task  # noqa: E402
from asep.orchestration import Agent, AgentResult, TaskGraph  # noqa: E402


@pytest.fixture
def state() -> RunState:
    return RunState(run_id="test", requirement="a requirement", scenario="test", mode="mock")


@pytest.fixture
def workspace(tmp_path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def task(task_id: str, agent: str = "noop", **kwargs) -> Task:
    kwargs.setdefault("title", task_id)
    return Task(id=task_id, agent=agent, **kwargs)


def graph(*tasks: Task) -> TaskGraph:
    return TaskGraph(list(tasks))


class RecordingAgent(Agent):
    """Agent that writes whatever it is told to, and records that it ran."""

    def __init__(self, name: str = "noop", writes: dict | None = None, error: Exception | None = None):
        self.name = name
        self._writes = writes or {}
        self._error = error
        self.calls = 0

    def run(self, ctx):
        self.calls += 1
        if self._error is not None and self.calls == 1:
            raise self._error
        return AgentResult(writes=dict(self._writes), note=f"{self.name} ran")


__all__ = ["RecordingAgent", "RiskLevel", "graph", "task"]
