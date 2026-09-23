"""Operational metrics for a run, derived from its own trace.

The trace records what happened. Metrics turn that into numbers you can compare
between runs, which is the difference between "the run succeeded" and knowing
whether it is getting slower, retrying more, or repairing more often.

Derived rather than instrumented: every figure here comes from events and task
records the engine already produces, so nothing has to be kept in step with a
counter that someone will forget to increment.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .models import CheckStatus, EventType, RunState, Severity, TaskStatus


@dataclass
class AgentMetrics:
    invocations: int = 0
    succeeded: int = 0
    failed: int = 0
    retries: int = 0
    total_ms: int = 0

    @property
    def mean_ms(self) -> int:
        return self.total_ms // self.invocations if self.invocations else 0

    def as_dict(self) -> dict:
        return {
            "invocations": self.invocations,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "retries": self.retries,
            "total_ms": self.total_ms,
            "mean_ms": self.mean_ms,
        }


@dataclass
class RunMetrics:
    run_id: str
    scenario: str
    status: str
    duration_ms: int

    tasks_planned: int = 0
    tasks_injected: int = 0
    tasks_succeeded: int = 0
    tasks_failed: int = 0
    tasks_skipped: int = 0
    tasks_rejected: int = 0

    retries: int = 0
    escalations: int = 0
    repair_rounds: int = 0
    approvals_requested: int = 0
    approvals_denied: int = 0

    validation_rounds: int = 0
    checks_run: int = 0
    checks_failed: int = 0
    findings: dict[str, int] = field(default_factory=dict)

    artifacts: int = 0
    artifact_lines: int = 0

    agents: dict[str, AgentMetrics] = field(default_factory=dict)

    @property
    def critical_path_ms(self) -> int:
        """Time in the slowest agent, as a share of total wall clock."""
        return max((a.total_ms for a in self.agents.values()), default=0)

    @property
    def first_pass_yield(self) -> bool:
        """Did the run reach a passing verdict without needing a repair?"""
        return self.repair_rounds == 0 and self.status == "succeeded"

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "scenario": self.scenario,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "first_pass_yield": self.first_pass_yield,
            "tasks": {
                "planned": self.tasks_planned,
                "injected_at_runtime": self.tasks_injected,
                "succeeded": self.tasks_succeeded,
                "failed": self.tasks_failed,
                "skipped": self.tasks_skipped,
                "rejected": self.tasks_rejected,
            },
            "reliability": {
                "retries": self.retries,
                "escalations": self.escalations,
                "repair_rounds": self.repair_rounds,
            },
            "oversight": {
                "approvals_requested": self.approvals_requested,
                "approvals_denied": self.approvals_denied,
            },
            "validation": {
                "rounds": self.validation_rounds,
                "checks_run": self.checks_run,
                "checks_failed": self.checks_failed,
                "findings_by_severity": self.findings,
            },
            "output": {
                "artifacts": self.artifacts,
                "lines": self.artifact_lines,
            },
            "agents": {name: m.as_dict() for name, m in sorted(self.agents.items())},
        }


def collect(state: RunState) -> RunMetrics:
    metrics = RunMetrics(
        run_id=state.run_id,
        scenario=state.scenario,
        status=state.status.value,
        duration_ms=state.duration_ms,
    )

    for task in state.tasks.values():
        if task.origin == "repair":
            metrics.tasks_injected += 1
        else:
            metrics.tasks_planned += 1

        if task.status is TaskStatus.SUCCEEDED:
            metrics.tasks_succeeded += 1
        elif task.status is TaskStatus.FAILED:
            metrics.tasks_failed += 1
        elif task.status is TaskStatus.SKIPPED:
            metrics.tasks_skipped += 1
        elif task.status is TaskStatus.REJECTED:
            metrics.tasks_rejected += 1

        agent = metrics.agents.setdefault(task.agent, AgentMetrics())
        agent.invocations += 1
        agent.total_ms += task.duration_ms
        agent.retries += max(0, task.attempts - 1)
        if task.status is TaskStatus.SUCCEEDED:
            agent.succeeded += 1
        elif task.status is TaskStatus.FAILED:
            agent.failed += 1

    for event in state.events:
        if event.type is EventType.TASK_RETRIED:
            metrics.retries += 1
        elif event.type is EventType.ESCALATED:
            metrics.escalations += 1
        elif event.type is EventType.REPAIR_SCHEDULED:
            metrics.repair_rounds += 1
        elif event.type is EventType.APPROVAL_REQUESTED:
            metrics.approvals_requested += 1
        elif event.type is EventType.APPROVAL_DENIED:
            metrics.approvals_denied += 1

    report = state.blackboard.get("final_validation_report") or state.blackboard.get(
        "validation_report"
    )
    if report is not None:
        metrics.validation_rounds = report.attempt
        metrics.checks_run = len(report.checks)
        metrics.checks_failed = sum(
            1 for c in report.checks if c.status is CheckStatus.FAIL
        )
        severities: Counter[str] = Counter()
        for check in report.checks:
            for finding in check.findings:
                severities[finding.severity.value] += 1
        metrics.findings = {
            level.value: severities.get(level.value, 0) for level in Severity
        }

    metrics.artifacts = len(state.artifacts)
    metrics.artifact_lines = sum(a.lines for a in state.artifacts.values())
    return metrics


def render(metrics: RunMetrics) -> str:
    """A compact operator-facing summary."""
    slowest = sorted(
        metrics.agents.items(), key=lambda kv: kv[1].total_ms, reverse=True
    )[:3]

    lines = [
        f"# Run metrics — {metrics.run_id}",
        "",
        f"`{metrics.scenario}` finished **{metrics.status}** in {metrics.duration_ms}ms.",
        "",
        "| Signal | Value |",
        "| --- | --- |",
        f"| first-pass yield | {'yes' if metrics.first_pass_yield else 'no'} |",
        f"| tasks planned / injected | {metrics.tasks_planned} / {metrics.tasks_injected} |",
        f"| retries | {metrics.retries} |",
        f"| repair rounds | {metrics.repair_rounds} |",
        f"| escalations | {metrics.escalations} |",
        f"| approvals requested / denied | "
        f"{metrics.approvals_requested} / {metrics.approvals_denied} |",
        f"| validation rounds | {metrics.validation_rounds} |",
        f"| checks run / failed | {metrics.checks_run} / {metrics.checks_failed} |",
        f"| artifacts / lines | {metrics.artifacts} / {metrics.artifact_lines} |",
        "",
    ]

    if slowest:
        lines += [
            "## Where the time went",
            "",
            "| Agent | Calls | Total | Mean | Retries |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{name}` | {m.invocations} | {m.total_ms}ms | {m.mean_ms}ms | {m.retries} |"
            for name, m in slowest
        ]
        lines += [""]

    lines += [
        "`first-pass yield` is the one to watch across runs: it says whether the "
        "platform got to a passing verdict without needing to repair itself. A "
        "fall in that number is the signal that output quality is drifting, and "
        "it moves before the pass/fail result does.",
        "",
    ]
    return "\n".join(lines)


__all__ = ["AgentMetrics", "RunMetrics", "collect", "render"]
