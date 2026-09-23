from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from ..models import ApprovalDecision, RiskLevel, Task

# Actions an agent may never take, regardless of risk tier or human approval.
# Autonomy is bounded by capability, not only by review.
FORBIDDEN_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brm\s+-rf\s+/",
        r"\bDROP\s+(TABLE|DATABASE)\b",
        r"\bTRUNCATE\s+TABLE\b",
        r"\bkubectl\s+(delete|apply)\b",
        r"\bterraform\s+(apply|destroy)\b",
        r"\bgit\s+push\b",
        r"\baws\s+\w+\s+delete",
        # Matches the command, not the English noun: prose legitimately says
        # "flush on shutdown", and a guardrail that fires on documentation gets
        # switched off, which costs more than it protects.
        r"\b(?:sudo\s+)?shutdown\s+(?:[-/]\w|now\b)",
        r"\bcurl\b.*\|\s*(ba)?sh",
    )
]

# Agents allowed to run without a human in the loop. Everything producing a
# durable change to a real system is gated.
AUTONOMOUS_AGENTS = {
    "requirement",
    "planner",
    "architecture",
    "codebase",
    "reproduction",
    "api_design",
    "test",
    "documentation",
    "validation",
    "summary",
}

GATED_AGENTS = {
    "implementation": RiskLevel.MEDIUM,
    "repair": RiskLevel.MEDIUM,
}


class ApprovalHandler(Protocol):
    def __call__(self, task: Task, context: str) -> ApprovalDecision: ...


@dataclass
class Policy:
    """Decides what runs unattended and what stops for a human.

    `approve_threshold` is the risk level at or above which a human must decide.
    Default MEDIUM: analysis is free, changes to code are reviewed.
    """

    approve_threshold: RiskLevel = RiskLevel.MEDIUM
    auto_approve: bool = False
    approver: str = "human"
    handler: ApprovalHandler | None = None
    decisions: list[ApprovalDecision] = field(default_factory=list)

    _ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}

    def classify(self, task: Task) -> RiskLevel:
        if task.agent in GATED_AGENTS:
            return max(
                task.risk, GATED_AGENTS[task.agent], key=lambda r: self._ORDER[r]
            )
        if task.agent in AUTONOMOUS_AGENTS:
            return task.risk
        return RiskLevel.HIGH

    def requires_approval(self, task: Task) -> bool:
        return self._ORDER[self.classify(task)] >= self._ORDER[self.approve_threshold]

    def request(self, task: Task, context: str = "") -> ApprovalDecision:
        if self.auto_approve or self.handler is None:
            decision = ApprovalDecision(
                task_id=task.id,
                approved=True,
                approver="auto-approve" if self.auto_approve else self.approver,
                reason="--yes supplied; gate recorded but not blocking"
                if self.auto_approve
                else "no interactive handler configured",
            )
        else:
            decision = self.handler(task, context)
        self.decisions.append(decision)
        return decision

    @staticmethod
    def screen(content: str) -> list[str]:
        """Return forbidden operations present in generated content."""
        return [p.pattern for p in FORBIDDEN_PATTERNS if p.search(content)]


def cli_approval_handler(prompt: Callable[[str], str] = input) -> ApprovalHandler:
    def handler(task: Task, context: str) -> ApprovalDecision:
        print(f"\n  APPROVAL REQUIRED  {task.id}  {task.title}")
        print(f"  agent={task.agent}  risk={task.risk.value}")
        if context:
            for line in context.splitlines():
                print(f"    {line}")
        answer = prompt("  approve? [y/N] ").strip().lower()
        return ApprovalDecision(
            task_id=task.id,
            approved=answer in ("y", "yes"),
            approver="human",
            reason="interactive cli decision",
        )

    return handler
