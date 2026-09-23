"""Persisting a run, and picking one back up.

`trace.jsonl` is the replayable record. `run.json` also carries the blackboard,
type-tagged so values rehydrate as models rather than bare dicts, which is what
makes a halted run resumable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..models import (
    ApiContract,
    Architecture,
    ImpactAnalysis,
    NormalizedRequirement,
    RunState,
    ValidationReport,
)

# Blackboard values that are models rather than plain data. Anything not listed
# round-trips as JSON, which is correct for the dicts and lists agents publish.
REHYDRATE: dict[str, type[BaseModel]] = {
    "NormalizedRequirement": NormalizedRequirement,
    "Architecture": Architecture,
    "ApiContract": ApiContract,
    "ImpactAnalysis": ImpactAnalysis,
    "ValidationReport": ValidationReport,
}


def _encode(value: Any) -> dict:
    if isinstance(value, BaseModel):
        return {"type": type(value).__name__, "value": json.loads(value.model_dump_json())}
    return {"type": "json", "value": value}


def _decode(entry: dict) -> Any:
    model = REHYDRATE.get(entry.get("type", "json"))
    if model is None:
        return entry.get("value")
    return model.model_validate(entry["value"])


class RunRecorder:
    def __init__(self, root: Path, run_id: str):
        self.dir = root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.dir / "trace.jsonl"
        self.run_path = self.dir / "run.json"
        self._written = 0

    def flush_events(self, state: RunState) -> None:
        new = state.events[self._written :]
        if not new:
            return
        with self.trace_path.open("a", encoding="utf-8") as fh:
            for event in new:
                fh.write(event.model_dump_json() + "\n")
        self._written = len(state.events)

    def save(self, state: RunState) -> Path:
        self.flush_events(state)
        payload = json.loads(state.model_dump_json(exclude={"blackboard"}))
        payload["blackboard_keys"] = state.blackboard.keys
        payload["blackboard"] = {
            key: _encode(value) for key, value in state.blackboard.data.items()
        }
        self.run_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return self.run_path

    def write_text(self, name: str, content: str) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    # ------------------------------------------------------------- resuming

    @classmethod
    def load(cls, root: Path, run_id: str) -> RunState:
        """Rebuild a run from its record.

        Succeeded tasks stay succeeded. Everything else returns to pending,
        including failures — what broke them may be what the human just supplied.
        """
        path = Path(root) / run_id / "run.json"
        if not path.exists():
            raise FileNotFoundError(f"no run record at {path}")

        payload = json.loads(path.read_text(encoding="utf-8"))
        stored = payload.pop("blackboard", {})
        payload.pop("blackboard_keys", None)

        state = RunState.model_validate(payload)
        for key, entry in stored.items():
            state.blackboard.put(key, _decode(entry))

        for task in state.tasks.values():
            if task.status.value != "succeeded":
                task.status = task.status.__class__("pending")
                task.attempts = 0
                task.error = None
        return state

    @property
    def event_count(self) -> int:
        return self._written

    def adopt(self, state: RunState) -> None:
        """Continue appending to an existing trace rather than duplicating it."""
        self._written = len(state.events)


__all__ = ["REHYDRATE", "RunRecorder"]
