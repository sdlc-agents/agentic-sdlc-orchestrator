"""Turn a sentence into an engineering problem statement.

The interesting part is what the sentence does not say. Ambiguities are named,
then answered by a human, resolved by a recorded default, or allowed to halt the
run.
"""

from __future__ import annotations

from ..models import ArtifactKind, Assumption, Clarity, NormalizedRequirement
from ..orchestration.engine import AgentContext, AgentResult
from ..orchestration.errors import ClarificationRequired
from .base import ProviderAgent

INSTRUCTION = """Normalize this requirement into an explicit problem statement.

Separate what the requester said from what you are inferring. Produce numbered
functional and non-functional requirements. Name every ambiguity that would
change the design if answered differently, and mark an ambiguity as blocking
only when no reasonable default exists — a blocking ambiguity stops the run and
costs a human their attention. Give a default assumption wherever a competent
engineer would have one. List what you are deliberately leaving out of scope."""


class RequirementAgent(ProviderAgent):
    name = "requirement"
    schema = NormalizedRequirement
    instruction = INSTRUCTION

    def __init__(self, assume_defaults: bool = True, answers: dict[str, str] | None = None):
        # `assume_defaults` is the autonomy dial for this stage: with it off, any
        # blocking ambiguity halts the run and waits for a person.
        self.assume_defaults = assume_defaults
        self.answers = answers or {}

    def run(self, ctx: AgentContext) -> AgentResult:
        requirement: NormalizedRequirement = self.call(  # type: ignore[assignment]
            ctx, raw=ctx.state.requirement, scenario=ctx.state.scenario
        )

        resolved = self._resolve(requirement)
        unresolved = [
            a
            for a in requirement.blocking_ambiguities
            if a.id not in {asm.resolves for asm in requirement.assumptions}
        ]
        if unresolved:
            raise ClarificationRequired(
                [f"{a.id}: {a.question} (matters because {a.why_it_matters})" for a in unresolved]
            )

        # Every ambiguity now has an answer or a recorded default; anything
        # else raised above rather than reaching here.
        requirement.clarity = Clarity.CLEAR

        doc = self.artifact(
            path="docs/requirements.md",
            content=_render(requirement),
            produced_by=ctx.task.id,
            kind=ArtifactKind.DOC,
            language="markdown",
            covers=requirement.requirement_ids,
        )
        self.workspace(ctx).write(doc.path, doc.content)

        return AgentResult(
            writes={"requirement": requirement},
            artifacts=[doc],
            note=(
                f"normalized into {len(requirement.functional)} functional and "
                f"{len(requirement.non_functional)} non-functional requirements; "
                f"{len(requirement.ambiguities)} ambiguity(ies), {resolved} resolved"
            ),
        )

    def _resolve(self, requirement: NormalizedRequirement) -> int:
        """Attach an assumption to every ambiguity we are allowed to answer."""
        next_id = len(requirement.assumptions) + 1
        resolved = 0
        for ambiguity in requirement.ambiguities:
            answer = self.answers.get(ambiguity.id)
            if answer:
                statement, source = answer, "human"
            elif ambiguity.default_assumption and (
                self.assume_defaults or not ambiguity.blocking
            ):
                statement, source = ambiguity.default_assumption, "default"
            else:
                continue

            requirement.assumptions.append(
                Assumption(
                    id=f"ASM-{next_id:03d}",
                    statement=statement,
                    source=source,  # type: ignore[arg-type]
                    resolves=ambiguity.id,
                )
            )
            next_id += 1
            resolved += 1
        return resolved


def _render(r: NormalizedRequirement) -> str:
    lines = [
        "# Requirements",
        "",
        "## Requested",
        "",
        f"> {r.raw}",
        "",
        "## Interpreted intent",
        "",
        r.intent,
        "",
        f"Kind: {r.kind.value}",
        "",
        "## Functional",
        "",
        "| ID | Priority | Requirement |",
        "| --- | --- | --- |",
    ]
    lines += [f"| {f.id} | {f.priority} | {f.statement} |" for f in r.functional]
    lines += [
        "",
        "## Non-functional",
        "",
        "| ID | Category | Requirement | Target |",
        "| --- | --- | --- | --- |",
    ]
    lines += [
        f"| {n.id} | {n.category} | {n.statement} | {n.target or '—'} |"
        for n in r.non_functional
    ]

    if r.ambiguities:
        lines += [
            "",
            "## Ambiguities",
            "",
            "What the request did not say, and what was done about it.",
            "",
        ]
        by_ambiguity = {a.resolves: a for a in r.assumptions if a.resolves}
        for ambiguity in r.ambiguities:
            assumption = by_ambiguity.get(ambiguity.id)
            lines += [
                f"### {ambiguity.id} — {ambiguity.question}",
                "",
                f"Blocking: {'yes' if ambiguity.blocking else 'no'}. "
                f"Matters because {ambiguity.why_it_matters}",
                "",
                (
                    f"Resolved by {assumption.source}: {assumption.statement}"
                    if assumption
                    else "Unresolved."
                ),
                "",
            ]

    if r.out_of_scope:
        lines += ["## Out of scope", ""] + [f"- {item}" for item in r.out_of_scope] + [""]
    return "\n".join(lines)


__all__ = ["RequirementAgent"]
