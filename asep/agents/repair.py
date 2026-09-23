"""Close validation findings without re-running the pipeline.

Scheduled by the engine rather than the plan, and given structured findings. The
re-validation that follows decides whether the defect is actually gone.
"""

from __future__ import annotations

from ..models import Artifact, ArtifactKind
from ..orchestration.engine import AgentContext, AgentResult
from ..orchestration.errors import ContractError
from .base import ProviderAgent
from .schemas import RepairPlan

INSTRUCTION = """Fix exactly the findings you are given.

Each edit must name the finding it addresses. Do not refactor, rename, reformat
or improve anything you were not asked about — an unrelated change in a repair
round is indistinguishable from a regression to whoever reviews this. If a
finding cannot be fixed by an edit, list it as unrepairable rather than guessing;
it will be escalated to a human, which is the correct outcome."""


class RepairAgent(ProviderAgent):
    name = "repair"
    schema = RepairPlan
    instruction = INSTRUCTION

    def run(self, ctx: AgentContext) -> AgentResult:
        findings = ctx.findings
        if not findings:
            raise ContractError("repair was scheduled with no open findings")

        plan: RepairPlan = self.call(  # type: ignore[assignment]
            ctx,
            findings=findings,
            artifacts_index=ctx.read("artifacts_index"),
            api_contract=ctx.read("api_contract"),
        )

        if not plan.edits:
            raise ContractError(
                f"repair produced no edits for {len(findings)} finding(s): "
                f"{'; '.join(plan.unrepairable) or plan.summary}"
            )

        workspace = self.workspace(ctx)
        touched: dict[str, str] = {}

        for edit in plan.edits:
            current = touched.get(edit.path)
            if current is None:
                current = workspace.read(edit.path) if workspace.exists(edit.path) else ""

            if edit.action == "create":
                updated = edit.content
            elif edit.action == "append":
                if not current:
                    raise ContractError(
                        f"repair wants to append to {edit.path}, which does not exist"
                    )
                updated = current.rstrip("\n") + "\n" + edit.content.lstrip("\n")
            else:  # replace
                if not edit.anchor:
                    raise ContractError(
                        f"repair edit on {edit.path} is a replace with no anchor"
                    )
                if edit.anchor not in current:
                    # Retryable: the model gets the message and can pick a real
                    # anchor rather than the one it imagined.
                    raise ContractError(
                        f"repair anchor not found in {edit.path}: {edit.anchor[:80]!r}"
                    )
                updated = current.replace(edit.anchor, edit.content, 1)

            touched[edit.path] = updated

        artifacts: list[Artifact] = []
        for path, content in touched.items():
            workspace.write(path, content)
            previous = ctx.state.artifacts.get(path)
            artifacts.append(
                self.artifact(
                    path=path,
                    content=content,
                    produced_by=ctx.task.id,
                    kind=previous.kind if previous else ArtifactKind.PATCH,
                    language=previous.language if previous else None,
                    covers=previous.covers if previous else [],
                )
            )

        index = dict(ctx.read("artifacts_index"))
        index.update(self.index(artifacts))

        summary = {
            "summary": plan.summary,
            "edits": [
                {"path": e.path, "action": e.action, "addresses": e.addresses}
                for e in plan.edits
            ],
            "unrepairable": plan.unrepairable,
            "findings_in": len(findings),
        }

        note = (
            f"applied {len(plan.edits)} edit(s) across {len(touched)} file(s) for "
            f"{len(findings)} finding(s) — {plan.summary}"
        )
        if plan.unrepairable:
            note += f"; {len(plan.unrepairable)} left for a human"

        return AgentResult(
            writes={"repair_summary": summary, "artifacts_index": index},
            artifacts=artifacts,
            note=note,
        )


__all__ = ["RepairAgent"]
