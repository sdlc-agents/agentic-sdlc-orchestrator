"""Decompose the approved design into executable graph nodes.

The planner returns tasks, not prose, and the engine injects them into the graph
already running. Because the plan is data it can be checked first — a task
naming an unregistered agent is rejected here rather than at dispatch.
"""

from __future__ import annotations

from ..models import ArtifactKind, Task
from ..orchestration.engine import AgentContext, AgentResult
from ..orchestration.errors import ContractError
from .base import ProviderAgent
from .schemas import WorkPlan

INSTRUCTION = """Decompose this design into tasks for the execution graph.

Each task is bound to exactly one agent and declares the blackboard keys it
reads and writes; the engine refuses to run a task whose reads are absent, so an
incorrect contract is a planning error you will see immediately. Express
independence through dependencies: tasks that do not depend on each other will
be executed concurrently. Do not make an agent responsible for validating its
own output."""


class PlannerAgent(ProviderAgent):
    name = "planner"
    schema = WorkPlan
    instruction = INSTRUCTION

    def __init__(self, known_agents: set[str] | None = None):
        self.known_agents = known_agents or set()

    def run(self, ctx: AgentContext) -> AgentResult:
        plan: WorkPlan = self.call(  # type: ignore[assignment]
            ctx,
            requirement=ctx.read("requirement"),
            # Not every kind of change has a design stage: a bug fix and a
            # test-improvement run legitimately reach the planner without one.
            architecture=ctx.get("architecture"),
            api_contract=ctx.get("api_contract"),
            impact_analysis=ctx.get("impact_analysis"),
        )

        if not plan.tasks:
            raise ContractError("planner returned an empty plan")

        planned = set(plan.task_ids)
        for spec in plan.tasks:
            if self.known_agents and spec.agent not in self.known_agents:
                raise ContractError(
                    f"{spec.id} names agent '{spec.agent}', which is not registered "
                    f"(known: {sorted(self.known_agents)})"
                )
            unknown = [
                dep for dep in spec.depends_on if dep not in planned and dep not in ctx.state.tasks
            ]
            if unknown:
                raise ContractError(f"{spec.id} depends on unknown task(s): {unknown}")

        tasks = [
            Task(
                id=spec.id,
                title=spec.title,
                agent=spec.agent,
                # A task the planner left unanchored hangs off the planner
                # itself, so nothing it scheduled can start before it finished.
                depends_on=spec.depends_on or [ctx.task.id],
                covers=spec.covers,
                reads=spec.reads,
                writes=spec.writes,
                risk=spec.risk,
                rationale=spec.rationale,
            )
            for spec in plan.tasks
        ]

        doc = self.artifact(
            path="docs/plan.md",
            content=_render(plan, tasks),
            produced_by=ctx.task.id,
            kind=ArtifactKind.DOC,
            language="markdown",
        )
        self.workspace(ctx).write(doc.path, doc.content)

        concurrent = _concurrent_groups(tasks)
        return AgentResult(
            writes={"work_plan": plan},
            artifacts=[doc],
            follow_up_tasks=tasks,
            note=(
                f"planned {len(tasks)} task(s) for injection"
                + (f"; {concurrent} can run concurrently" if concurrent else "")
            ),
        )


def _concurrent_groups(tasks: list[Task]) -> str:
    by_deps: dict[tuple[str, ...], list[str]] = {}
    for task in tasks:
        by_deps.setdefault(tuple(sorted(task.depends_on)), []).append(task.id)
    groups = [ids for ids in by_deps.values() if len(ids) > 1]
    return "; ".join(" + ".join(sorted(g)) for g in groups)


def _render(plan: WorkPlan, tasks: list[Task]) -> str:
    lines = [
        "# Execution plan",
        "",
        "## Strategy",
        "",
        plan.strategy,
        "",
    ]
    if plan.parallelism_note:
        lines += ["## Concurrency", "", plan.parallelism_note, ""]

    lines += [
        "## Tasks",
        "",
        "| ID | Task | Agent | Depends on | Reads | Writes | Risk |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {t.id} | {t.title} | `{t.agent}` | {', '.join(t.depends_on) or '—'} | "
        f"{', '.join(f'`{k}`' for k in t.reads) or '—'} | "
        f"{', '.join(f'`{k}`' for k in t.writes) or '—'} | {t.risk.value} |"
        for t in tasks
    ]

    lines += ["", "## Graph", "", "```mermaid", "graph TD"]
    for task in tasks:
        lines.append(f'    {task.id}["{task.id}<br/>{task.agent}"]')
    for task in tasks:
        for dep in task.depends_on:
            lines.append(f"    {dep} --> {task.id}")
    lines += ["```", ""]
    return "\n".join(lines)


__all__ = ["PlannerAgent"]
