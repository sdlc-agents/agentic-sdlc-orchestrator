"""The agent roster.

Each agent is narrow on purpose: the stage that judges the output must not be
the stage that produced it.
"""

from __future__ import annotations

from ..orchestration.engine import Agent
from .base import ProviderAgent, summarize
from .build import DocumentationAgent, ImplementationAgent, TestAgent
from .codebase import CodebaseAgent
from .design import ApiDesignAgent, ArchitectureAgent
from .planning import PlannerAgent
from .repair import RepairAgent
from .reproduction import ReproductionAgent
from .requirement import RequirementAgent
from .schemas import (
    CodeBundle,
    GeneratedFile,
    RepairEdit,
    RepairPlan,
    RunSummary,
    TaskSpec,
    WorkPlan,
)
from .summary import SummaryAgent
from .validation import ValidationAgent

AGENT_CLASSES = (
    RequirementAgent,
    CodebaseAgent,
    ArchitectureAgent,
    ApiDesignAgent,
    PlannerAgent,
    ImplementationAgent,
    TestAgent,
    DocumentationAgent,
    ReproductionAgent,
    ValidationAgent,
    RepairAgent,
    SummaryAgent,
)

AGENT_NAMES = frozenset(cls.name for cls in AGENT_CLASSES)


def build_registry(
    assume_defaults: bool = True,
    answers: dict[str, str] | None = None,
    run_tests: bool = True,
    test_timeout_s: int = 180,
    extra_checks: tuple[str, ...] = (),
) -> dict[str, Agent]:
    """Instantiate every agent the engine can dispatch to.

    The planner is handed the registry's own key set, so a plan naming an agent
    that does not exist fails at planning time rather than at dispatch.
    """
    registry: dict[str, Agent] = {
        RequirementAgent.name: RequirementAgent(
            assume_defaults=assume_defaults, answers=answers
        ),
        CodebaseAgent.name: CodebaseAgent(),
        ArchitectureAgent.name: ArchitectureAgent(),
        ApiDesignAgent.name: ApiDesignAgent(),
        ImplementationAgent.name: ImplementationAgent(),
        TestAgent.name: TestAgent(),
        DocumentationAgent.name: DocumentationAgent(),
        ReproductionAgent.name: ReproductionAgent(test_timeout_s=test_timeout_s),
        ValidationAgent.name: ValidationAgent(
            run_tests=run_tests,
            test_timeout_s=test_timeout_s,
            extra_checks=extra_checks,
        ),
        RepairAgent.name: RepairAgent(),
        SummaryAgent.name: SummaryAgent(),
    }
    registry[PlannerAgent.name] = PlannerAgent(
        known_agents=set(registry) | {PlannerAgent.name}
    )
    return registry


__all__ = [
    "AGENT_CLASSES",
    "AGENT_NAMES",
    "ApiDesignAgent",
    "ArchitectureAgent",
    "CodeBundle",
    "CodebaseAgent",
    "DocumentationAgent",
    "GeneratedFile",
    "ImplementationAgent",
    "PlannerAgent",
    "ProviderAgent",
    "RepairAgent",
    "RepairEdit",
    "RepairPlan",
    "ReproductionAgent",
    "RequirementAgent",
    "RunSummary",
    "SummaryAgent",
    "TaskSpec",
    "TestAgent",
    "ValidationAgent",
    "WorkPlan",
    "build_registry",
    "summarize",
]
