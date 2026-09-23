"""Shared machinery for agents.

An agent states what it needs, asks the provider for one typed object, and turns
it into artifacts and blackboard writes. It decides nothing about what runs next
and never judges its own output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..models import Artifact, ArtifactKind
from ..orchestration.engine import Agent, AgentContext
from ..providers.base import GenerationRequest, Provider
from ..tools.workspace import Workspace

# Blackboard values are summarized before they are sent to a provider. Passing
# whole generated files back into a prompt wastes the budget on content the
# model already produced and drowns the part it needs to reason about.
MAX_CONTEXT_CHARS = 4000


def summarize(value: Any) -> Any:
    """Reduce a blackboard value to something worth putting in a prompt."""
    if isinstance(value, BaseModel):
        return json.loads(value.model_dump_json())
    if isinstance(value, dict):
        return {k: summarize(v) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [summarize(v) for v in value[:50]]
    if isinstance(value, str) and len(value) > MAX_CONTEXT_CHARS:
        return value[:MAX_CONTEXT_CHARS] + f"... [{len(value) - MAX_CONTEXT_CHARS} more chars]"
    return value


class ProviderAgent(Agent):
    """Base for agents whose work is one typed model call."""

    schema: type[BaseModel] = BaseModel
    instruction: str = ""

    def call(
        self,
        ctx: AgentContext,
        schema: type[BaseModel] | None = None,
        instruction: str | None = None,
        **context: Any,
    ) -> BaseModel:
        provider: Provider = ctx.provider  # type: ignore[assignment]
        request = GenerationRequest(
            agent=self.name,
            task_id=ctx.task.id,
            scenario=ctx.state.scenario,
            instruction=instruction or self.instruction,
            schema=schema or self.schema,
            context={k: summarize(v) for k, v in context.items()},
            attempt=ctx.attempt,
            previous_error=ctx.previous_error,
        )
        return provider.generate(request)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def workspace(ctx: AgentContext) -> Workspace:
        return Workspace(Path(ctx.workspace))

    @staticmethod
    def artifact(
        path: str,
        content: str,
        produced_by: str,
        kind: ArtifactKind = ArtifactKind.CODE,
        language: str | None = None,
        covers: list[str] | None = None,
    ) -> Artifact:
        """One artifact per path, so a repaired file replaces the version it
        fixed. The history stays in the trace."""
        return Artifact(
            id=path,
            kind=kind,
            path=path,
            content=content,
            produced_by=produced_by,
            language=language,
            covers=covers or [],
        )

    @staticmethod
    def index(artifacts: list[Artifact]) -> dict[str, dict]:
        return {
            a.path: {
                "id": a.id,
                "kind": a.kind.value,
                "produced_by": a.produced_by,
                "sha256": a.sha256[:12],
                "lines": a.lines,
                "covers": a.covers,
            }
            for a in artifacts
        }
