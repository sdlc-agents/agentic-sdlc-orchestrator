"""The agents that write files.

All three ask for a typed bundle, write it through `Workspace`, and publish an
index of what landed. None checks its own work; that belongs to validation.
"""

from __future__ import annotations

from ..models import Artifact, ArtifactKind
from ..orchestration.engine import AgentContext, AgentResult
from ..orchestration.errors import ContractError
from .base import ProviderAgent
from .schemas import CodeBundle

IMPLEMENTATION_INSTRUCTION = """Implement the system described by the contract and architecture.

Produce complete, runnable files — no placeholders, no `...`, no "implementation
left as an exercise". Honour the architecture's accepted costs: if it says a
write happens off a latency-critical path, do not put it back on that path.
Every endpoint declared in the contract must exist."""

TEST_INSTRUCTION = """Write an executable test suite for this contract.

Test against the contract, not against the implementation you were given, so the
suite can catch an implementation that does not satisfy it. Cover the failure
paths the contract declares status codes for, and the behaviour the architecture
depends on — including what the system is supposed to do under overload."""

DOCUMENTATION_INSTRUCTION = """Document this system for the engineer who inherits it.

Record the decisions and their accepted costs, not a description of the code.
State the known limits plainly; a document that only describes what works is not
useful to the person debugging it at 3am."""


class _BundleAgent(ProviderAgent):
    """Common body: request a bundle, write it, index it."""

    schema = CodeBundle
    writes_key = "artifacts_index"
    summary_key = "implementation_summary"
    default_kind = ArtifactKind.CODE

    def bundle(self, ctx: AgentContext) -> CodeBundle:  # pragma: no cover - interface
        raise NotImplementedError

    def run(self, ctx: AgentContext) -> AgentResult:
        bundle = self.bundle(ctx)
        if not bundle.files:
            raise ContractError(f"{self.name} returned a bundle with no files")

        workspace = self.workspace(ctx)
        artifacts: list[Artifact] = []
        for file in bundle.files:
            if not file.content.strip():
                raise ContractError(f"{self.name} produced an empty file: {file.path}")

            content = file.content
            if file.replaces is not None:
                if not workspace.exists(file.path):
                    raise ContractError(
                        f"{self.name} wants to edit {file.path}, which does not exist"
                    )
                current = workspace.read(file.path)
                if file.replaces not in current:
                    # Retryable: the model gets this message and can anchor on
                    # something that is actually in the file.
                    raise ContractError(
                        f"{self.name} anchored an edit on text not present in "
                        f"{file.path}: {file.replaces[:80]!r}"
                    )
                content = current.replace(file.replaces, file.content, 1)

            artifact = self.artifact(
                path=file.path,
                content=content,
                produced_by=ctx.task.id,
                kind=file.kind or self.default_kind,
                language=file.language,
                covers=file.covers,
            )
            workspace.write(artifact.path, artifact.content)
            artifacts.append(artifact)

        # Merge rather than replace: a brownfield run and a repair round both add
        # to an index that already describes files on disk.
        index = dict(ctx.get(self.writes_key) or {})
        index.update(self.index(artifacts))

        writes = {self.writes_key: index}
        if self.summary_key:
            writes[self.summary_key] = {
                "summary": bundle.summary,
                "notes": bundle.notes,
                "paths": bundle.paths,
            }

        total_lines = sum(a.lines for a in artifacts)
        return AgentResult(
            writes=writes,
            artifacts=artifacts,
            note=f"wrote {len(artifacts)} file(s), {total_lines} lines — {bundle.summary}",
        )


class ImplementationAgent(_BundleAgent):
    name = "implementation"
    instruction = IMPLEMENTATION_INSTRUCTION
    writes_key = "artifacts_index"
    summary_key = "implementation_summary"

    def bundle(self, ctx: AgentContext) -> CodeBundle:
        return self.call(  # type: ignore[return-value]
            ctx,
            api_contract=ctx.get("api_contract"),
            architecture=ctx.get("architecture"),
            impact_analysis=ctx.get("impact_analysis"),
            reproduction=ctx.get("reproduction"),
            requirement=ctx.get("requirement"),
        )


class TestAgent(_BundleAgent):
    name = "test"
    instruction = TEST_INSTRUCTION
    writes_key = "test_suite_index"
    summary_key = ""
    default_kind = ArtifactKind.TEST

    def bundle(self, ctx: AgentContext) -> CodeBundle:
        return self.call(  # type: ignore[return-value]
            ctx,
            api_contract=ctx.get("api_contract"),
            artifacts_index=ctx.get("artifacts_index"),
            architecture=ctx.get("architecture"),
            impact_analysis=ctx.get("impact_analysis"),
            requirement=ctx.get("requirement"),
        )


class DocumentationAgent(_BundleAgent):
    name = "documentation"
    instruction = DOCUMENTATION_INSTRUCTION
    writes_key = "documentation_index"
    summary_key = ""
    default_kind = ArtifactKind.DOC

    def bundle(self, ctx: AgentContext) -> CodeBundle:
        return self.call(  # type: ignore[return-value]
            ctx,
            architecture=ctx.get("architecture"),
            api_contract=ctx.get("api_contract"),
            impact_analysis=ctx.get("impact_analysis"),
            requirement=ctx.get("requirement"),
        )


__all__ = ["DocumentationAgent", "ImplementationAgent", "TestAgent"]
