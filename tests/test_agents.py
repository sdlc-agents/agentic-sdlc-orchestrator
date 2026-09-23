"""Agent behaviour that is not covered by running a whole scenario.

Mostly the refusals: the requirement agent refusing to guess, the planner
refusing a plan it cannot execute, the design agents refusing output that would
pass schema validation while being useless.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import task

from asep.agents import (
    ApiDesignAgent,
    ArchitectureAgent,
    PlannerAgent,
    RepairAgent,
    RequirementAgent,
    build_registry,
)
from asep.agents.schemas import RepairEdit, RepairPlan, TaskSpec, WorkPlan
from asep.models import ApiContract, Architecture, Endpoint
from asep.orchestration.engine import AgentContext
from asep.orchestration.errors import ClarificationRequired, ContractError
from asep.providers.base import GenerationRequest, Provider
from asep.providers.mock_provider import GREENFIELD, MockProvider


class StubProvider(Provider):
    """Returns whatever it was constructed with, and records the request."""

    name = "stub"

    def __init__(self, response):
        self.response = response
        self.requests: list[GenerationRequest] = []

    def generate(self, request):
        self.requests.append(request)
        return self.response


def context(agent_task, state, workspace, provider, **kwargs) -> AgentContext:
    return AgentContext(
        task=agent_task, state=state, workspace=Path(workspace), provider=provider, **kwargs
    )


class TestRequirementAgent:
    def _run(self, state, workspace, **agent_kwargs):
        agent = RequirementAgent(**agent_kwargs)
        ctx = context(task("T-001", agent="requirement"), state, workspace, MockProvider())
        state.scenario = GREENFIELD
        return agent.run(ctx)

    def test_defaults_are_recorded_as_assumptions_not_hidden(self, state, workspace):
        result = self._run(state, workspace, assume_defaults=True)
        requirement = result.writes["requirement"]

        assert requirement.assumptions, "a default that is not recorded is a silent guess"
        defaults = [a for a in requirement.assumptions if a.source == "default"]
        assert defaults
        assert all(a.resolves for a in requirement.assumptions)

    def test_without_defaults_a_blocking_ambiguity_halts_the_run(self, state, workspace):
        with pytest.raises(ClarificationRequired) as exc:
            self._run(state, workspace, assume_defaults=False)
        assert exc.value.questions
        assert "AMB-001" in exc.value.questions[0]

    def test_a_human_answer_outranks_the_default(self, state, workspace):
        result = self._run(
            state, workspace, assume_defaults=False, answers={"AMB-001": "50 rps, 10k links"}
        )
        requirement = result.writes["requirement"]
        answered = [a for a in requirement.assumptions if a.resolves == "AMB-001"]
        assert answered[0].source == "human"
        assert answered[0].statement == "50 rps, 10k links"

    def test_the_requirements_document_records_the_open_questions(self, state, workspace):
        result = self._run(state, workspace)
        doc = result.artifacts[0].content
        assert "## Ambiguities" in doc
        assert "AMB-001" in doc
        assert "Resolved by default" in doc


class TestDesignAgents:
    def test_an_architecture_with_no_tradeoffs_is_rejected(self, state, workspace):
        state.blackboard.put("requirement", None)
        agent = ArchitectureAgent()
        provider = StubProvider(Architecture(style="s", summary="s"))
        with pytest.raises(ContractError, match="trade-off"):
            agent.run(context(task("T-002", agent="architecture"), state, workspace, provider))

    def test_an_endpoint_with_no_requirement_trace_is_rejected(self, state, workspace):
        state.blackboard.put("requirement", None)
        contract = ApiContract(
            title="t", endpoints=[Endpoint(method="GET", path="/x", summary="untraced")]
        )
        with pytest.raises(ContractError, match="traceability"):
            ApiDesignAgent().run(
                context(task("T-003", agent="api_design"), state, workspace, StubProvider(contract))
            )

    def test_an_empty_contract_is_rejected(self, state, workspace):
        state.blackboard.put("requirement", None)
        with pytest.raises(ContractError, match="no endpoints"):
            ApiDesignAgent().run(
                context(
                    task("T-003", agent="api_design"),
                    state,
                    workspace,
                    StubProvider(ApiContract(title="t")),
                )
            )

    def test_the_published_spec_is_derived_from_the_contract(self, state, workspace):
        state.blackboard.put("requirement", None)
        contract = ApiContract(
            title="URL Shortener",
            endpoints=[
                Endpoint(method="GET", path="/healthz", summary="Liveness", covers=["FR-007"])
            ],
        )
        result = ApiDesignAgent().run(
            context(task("T-003", agent="api_design"), state, workspace, StubProvider(contract))
        )
        spec = result.artifacts[0].content
        assert "openapi" in spec and "/healthz" in spec and "URL Shortener" in spec


class TestPlanner:
    def _plan(self, tasks):
        return WorkPlan(strategy="s", tasks=tasks)

    def _ctx(self, state, workspace, plan):
        for key in ("requirement", "architecture", "api_contract"):
            state.blackboard.put(key, None)
        return context(task("T-004", agent="planner"), state, workspace, StubProvider(plan))

    def test_a_plan_naming_an_unregistered_agent_is_rejected(self, state, workspace):
        plan = self._plan([TaskSpec(id="T-010", title="t", agent="ghost")])
        agent = PlannerAgent(known_agents={"implementation", "planner"})
        with pytest.raises(ContractError, match="not registered"):
            agent.run(self._ctx(state, workspace, plan))

    def test_a_plan_with_a_dangling_dependency_is_rejected(self, state, workspace):
        plan = self._plan(
            [TaskSpec(id="T-010", title="t", agent="implementation", depends_on=["T-999"])]
        )
        with pytest.raises(ContractError, match="unknown task"):
            PlannerAgent().run(self._ctx(state, workspace, plan))

    def test_an_empty_plan_is_rejected(self, state, workspace):
        with pytest.raises(ContractError, match="empty plan"):
            PlannerAgent().run(self._ctx(state, workspace, self._plan([])))

    def test_unanchored_tasks_are_hung_off_the_planner(self, state, workspace):
        plan = self._plan([TaskSpec(id="T-010", title="t", agent="implementation")])
        result = PlannerAgent().run(self._ctx(state, workspace, plan))
        assert result.follow_up_tasks[0].depends_on == ["T-004"]

    def test_the_registry_and_the_planner_agree_on_the_agent_names(self):
        registry = build_registry()
        planner = registry["planner"]
        assert planner.known_agents >= set(registry)


class TestRepairAgent:
    def _ctx(self, state, workspace, plan, findings):
        state.blackboard.put("artifacts_index", {})
        state.blackboard.put("api_contract", None)
        return context(
            task("T-900-R1", agent="repair"),
            state,
            workspace,
            StubProvider(plan),
            findings=findings,
        )

    def test_repair_without_findings_is_a_contract_error(self, state, workspace):
        with pytest.raises(ContractError, match="no open findings"):
            RepairAgent().run(self._ctx(state, workspace, RepairPlan(summary="s"), []))

    def test_a_plan_with_no_edits_fails_rather_than_reporting_success(self, state, workspace):
        from asep.models import Finding, Severity

        finding = Finding(check="c", severity=Severity.ERROR, message="m", repair_hint="h")
        with pytest.raises(ContractError, match="no edits"):
            RepairAgent().run(
                self._ctx(state, workspace, RepairPlan(summary="cannot"), [finding])
            )

    def test_a_replace_whose_anchor_is_absent_is_retryable_not_silent(self, state, workspace):
        from asep.models import Finding, Severity
        from asep.tools.workspace import Workspace

        Workspace(Path(workspace)).write("app/x.py", "print('hello')\n")
        plan = RepairPlan(
            summary="s",
            edits=[
                RepairEdit(
                    path="app/x.py",
                    action="replace",
                    anchor="not present anywhere",
                    content="y",
                    addresses="m",
                )
            ],
        )
        finding = Finding(check="c", severity=Severity.ERROR, message="m", repair_hint="h")
        with pytest.raises(ContractError, match="anchor not found"):
            RepairAgent().run(self._ctx(state, workspace, plan, [finding]))

    def test_edits_are_applied_and_reported_against_their_finding(self, state, workspace):
        from asep.models import Finding, Severity
        from asep.tools.workspace import Workspace

        ws = Workspace(Path(workspace))
        ws.write("app/x.py", "import os\n")
        plan = RepairPlan(
            summary="added the thing",
            edits=[
                RepairEdit(
                    path="app/x.py", action="replace", anchor="import os",
                    content="import os, sys", addresses="finding one",
                ),
                RepairEdit(
                    path="app/x.py", action="append", content="\nprint(sys.path)\n",
                    addresses="finding one",
                ),
            ],
        )
        finding = Finding(check="c", severity=Severity.ERROR, message="m", repair_hint="h")
        result = RepairAgent().run(self._ctx(state, workspace, plan, [finding]))

        content = ws.read("app/x.py")
        assert "import os, sys" in content and "print(sys.path)" in content
        assert result.writes["repair_summary"]["edits"][0]["addresses"] == "finding one"
        # Both edits touched one file, so one artifact describes its final state.
        assert len(result.artifacts) == 1
