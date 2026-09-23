"""The run report, assembled from state rather than generated.

`build` is pure so the runner can re-render it after the engine stops. The
summary task runs inside the run it describes, so its own view of the outcome
is necessarily incomplete.
"""

from __future__ import annotations

from ..models import (
    ApiContract,
    Architecture,
    Artifact,
    ArtifactKind,
    CheckStatus,
    NormalizedRequirement,
    RunState,
    TaskStatus,
    ValidationReport,
)
from ..orchestration.engine import Agent, AgentContext, AgentResult
from .base import ProviderAgent
from .schemas import RunSummary

SUMMARY_PATH = "summary.md"


class SummaryAgent(Agent):
    name = "summary"

    def run(self, ctx: AgentContext) -> AgentResult:
        summary, rendered = build(ctx.state, produced_by=ctx.task.id)
        return AgentResult(
            writes={"run_summary": summary},
            artifacts=[rendered],
            note=(
                f"summarized {len(ctx.state.tasks)} task(s), "
                f"{len(ctx.state.artifacts)} artifact(s), "
                f"{len(summary.open_risks)} open risk(s)"
            ),
        )


def build(state: RunState, produced_by: str) -> tuple[RunSummary, Artifact]:
    """Render the run report from state alone."""
    requirement: NormalizedRequirement = state.blackboard.require("requirement")
    architecture: Architecture | None = state.blackboard.get("architecture")
    contract: ApiContract | None = state.blackboard.get("api_contract")
    report: ValidationReport | None = state.blackboard.get(
        "final_validation_report"
    ) or state.blackboard.get("validation_report")
    repair = state.blackboard.get("repair_summary")

    code_files = [
        a
        for a in state.artifacts.values()
        if a.kind in (ArtifactKind.CODE, ArtifactKind.TEST)
    ]
    succeeded = len([t for t in state.tasks.values() if t.status == TaskStatus.SUCCEEDED])

    plan = state.blackboard.get("work_plan")
    summary = RunSummary(
        headline=(
            f"{requirement.intent} — {len(state.artifacts)} artifact(s) from "
            f"{succeeded} completed task(s)"
        ),
        what_was_built=[
            f"{len(code_files)} source and test file(s), "
            f"{sum(a.lines for a in code_files)} lines",
            *(
                [f"{len(contract.endpoints)} endpoint(s) under contract v{contract.version}"]
                if contract
                else []
            ),
            *([f"architecture: {architecture.style}"] if architecture else []),
        ],
        key_decisions=[
            f"{t.decision}: chose {t.chosen} — {t.accepted_cost}"
            for t in (architecture.trade_offs if architecture else [])
        ],
        open_risks=[
            f"{r.id} ({r.severity.value}): {r.description} — {r.mitigation}"
            for r in (architecture.risks if architecture else [])
            if r.severity.value in ("medium", "high")
        ],
        next_steps=_next_steps(requirement, report),
        limitations=_limitations(state, requirement, report, contract),
    )

    artifact = ProviderAgent.artifact(
        path=SUMMARY_PATH,
        content=_render(state, summary, requirement, report, repair, plan),
        produced_by=produced_by,
        kind=ArtifactKind.REPORT,
        language="markdown",
    )
    return summary, artifact


def _next_steps(requirement: NormalizedRequirement, report: ValidationReport | None) -> list[str]:
    steps: list[str] = []
    if report is not None and report.status == CheckStatus.FAIL:
        steps.append(
            f"Resolve {len(report.errors)} outstanding validation error(s) before this "
            "change is merged"
        )
    steps += [
        f"Confirm the assumption behind {a.resolves}: {a.statement}"
        for a in requirement.assumptions
        if a.source == "default"
    ]
    if report is not None and report.warnings:
        steps.append(
            f"Review {len(report.warnings)} traceability warning(s) — requirements no "
            "artifact claims to cover"
        )
    steps += [f"Out of scope, still unbuilt: {item}" for item in requirement.out_of_scope[:3]]
    return steps


def _limitations(
    state: RunState,
    requirement: NormalizedRequirement,
    report: ValidationReport | None,
    contract,
) -> list[str]:
    """What this run does not establish.

    Assembled from the run rather than written down once, because the honest
    limits change with what actually happened: a repair budget that was never
    touched is not a limitation, and one that was exhausted very much is.
    """
    limits: list[str] = []

    tests_check = None
    if report is not None:
        tests_check = next((c for c in report.checks if c.name == "tests"), None)
    if tests_check is None or tests_check.status is not CheckStatus.PASS:
        limits.append(
            "The generated suite did not pass, so nothing here is evidence that "
            "the deliverable behaves correctly."
        )
    else:
        limits.append(
            f"Correctness is established only as far as the suite asserts it "
            f"({tests_check.detail}). A passing suite bounds risk; it does not "
            "eliminate it."
        )

    limits.append(
        "No performance, load or security testing was run, so any latency, "
        "throughput or security target in the requirements is design intent "
        "rather than a measured result."
    )

    if contract is not None:
        limits.append(
            "Contract conformance is structural: every declared endpoint exists, "
            "but request and response bodies are not schema-validated against the "
            "published spec."
        )

    defaults = [a for a in requirement.assumptions if a.source == "default"]
    if defaults:
        limits.append(
            f"{len(defaults)} requirement(s) were resolved by an assumed default "
            "rather than by a human answer; if any of those assumptions is wrong, "
            "the design built on it is wrong."
        )

    if report is not None and report.warnings:
        limits.append(
            f"{len(report.warnings)} warning(s) were reported and not acted on."
        )

    escalated = [
        e for e in state.events if e.type.value == "escalated"
    ]
    if escalated:
        limits.append(
            f"{len(escalated)} issue(s) were escalated to a human and remain open."
        )

    if requirement.out_of_scope:
        limits.append(
            "Deliberately out of scope: " + "; ".join(requirement.out_of_scope) + "."
        )

    limits.append(
        "The deliverable is a prototype. It has not been deployed, so migrations, "
        "startup and shutdown are untested outside the test harness."
    )
    return limits


def _render(
    state: RunState,
    summary: RunSummary,
    requirement: NormalizedRequirement,
    report: ValidationReport | None,
    repair: dict | None,
    plan=None,
) -> str:
    counts = state.counts()
    lines = [
        "# Run summary",
        "",
        f"**Run `{state.run_id}` — {state.status.value}** in {state.duration_ms}ms",
        "",
        f"> {state.requirement}",
        "",
        summary.headline,
        "",
    ]

    if plan is not None:
        lines += [
            "## Implementation plan and rationale",
            "",
            getattr(plan, "strategy", ""),
            "",
        ]
        note = getattr(plan, "parallelism_note", "")
        if note:
            lines += [f"**Sequencing.** {note}", ""]
        lines += [
            "Each task below states why it exists. Tasks marked `repair` were not "
            "planned — the engine added them to the running graph after validation "
            "found a defect.",
            "",
            "| Task | Why it is in the plan |",
            "| --- | --- |",
        ]
        lines += [
            f"| {t.id} | {t.rationale} |"
            for t in sorted(state.tasks.values(), key=lambda t: t.id)
            if t.rationale
        ]
        lines += [""]

    lines += [
        "## Execution",
        "",
        "| Status | Tasks |",
        "| --- | --- |",
    ]
    lines += [f"| {status} | {count} |" for status, count in sorted(counts.items())]

    lines += [
        "",
        "| Task | Agent | Status | Origin | Attempts | Time |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {t.id} | `{t.agent}` | {t.status.value} | {t.origin} | {t.attempts} | "
        f"{t.duration_ms}ms |"
        for t in sorted(state.tasks.values(), key=lambda t: t.id)
    ]

    if state.approvals:
        lines += [
            "",
            "## Approvals",
            "",
            "| Task | Decision | Approver | Reason |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| {a.task_id} | {'approved' if a.approved else 'REJECTED'} | {a.approver} | "
            f"{a.reason} |"
            for a in state.approvals
        ]

    lines += ["", "## What was built", ""]
    lines += [f"- {item}" for item in summary.what_was_built]

    if state.artifacts:
        lines += [
            "",
            "### Generated artifacts",
            "",
            "Every file this run produced, with the task that produced it and the "
            "requirements it claims to cover. A claim of coverage is traceability, "
            "not proof.",
            "",
            "| Artifact | Kind | Lines | Produced by | Covers |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{a.path}` | {a.kind.value} | {a.lines} | {a.produced_by} | "
            + (", ".join(a.covers) or "—")
            + " |"
            for a in sorted(
                state.artifacts.values(), key=lambda a: (a.kind.value, a.path)
            )
        ]

    if summary.key_decisions:
        lines += ["", "## Decisions and what they cost", ""]
        lines += [f"- {item}" for item in summary.key_decisions]

    if repair:
        lines += ["", "## Repairs", "", str(repair.get("summary", "")), ""]
        lines += [
            f"- `{e['path']}` ({e['action']}) — {e['addresses']}"
            for e in repair.get("edits", [])
        ]
        if repair.get("unrepairable"):
            lines += ["", "Left for a human:", ""]
            lines += [f"- {item}" for item in repair["unrepairable"]]

    if report is not None:
        lines += [
            "",
            "## Verification",
            "",
            f"Validation {report.status.value} after {report.attempt} round(s). "
            "These checks parsed the files on disk and executed the generated suite; "
            "none of them asked an agent whether its own work was correct.",
            "",
            "| Check | Status | Detail |",
            "| --- | --- | --- |",
        ]
        lines += [f"| {c.name} | {c.status.value} | {c.detail} |" for c in report.checks]

    if summary.open_risks:
        lines += ["", "## Open risks", ""]
        lines += [f"- {item}" for item in summary.open_risks]

    if requirement.assumptions:
        lines += ["", "## Assumptions this run made", ""]
        lines += [
            f"- **{a.source}** ({a.resolves}): {a.statement}" for a in requirement.assumptions
        ]

    if summary.limitations:
        lines += [
            "",
            "## Limitations",
            "",
            "What this run does not establish. Stated because a report that only "
            "lists what passed invites the reader to assume the rest was covered.",
            "",
        ]
        lines += [f"- {item}" for item in summary.limitations]
        lines += [
            "",
            "The full validation approach — the test strategy, what each check "
            "proves, and what none of them can see — is in "
            "`validation-approach.md` beside this file.",
        ]

    if summary.next_steps:
        lines += ["", "## Next steps", ""]
        lines += [f"- {item}" for item in summary.next_steps]

    lines += [""]
    return "\n".join(lines)


__all__ = ["SUMMARY_PATH", "SummaryAgent", "build"]
