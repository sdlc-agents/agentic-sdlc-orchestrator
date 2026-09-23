"""The orchestration guarantees.

Each test here pins down a behaviour that distinguishes an orchestrator from a
loop that calls agents in order: contracts are enforced, failures are classified
rather than retried blindly, a rejection stops the work downstream of it, and the
graph can grow while it runs.
"""

from __future__ import annotations

from pathlib import Path

from conftest import RecordingAgent, graph, task

from asep.models import (
    ApprovalDecision,
    ArtifactKind,
    CheckResult,
    CheckStatus,
    EventType,
    Finding,
    RunStatus,
    Severity,
    TaskStatus,
    ValidationReport,
)
from asep.orchestration import (
    Agent,
    AgentResult,
    ClarificationRequired,
    Engine,
    Policy,
    TransientError,
)


def engine_for(g, agents, state, workspace, **kwargs):
    kwargs.setdefault("policy", Policy(auto_approve=True))
    return Engine(
        graph=g, agents=agents, state=state, workspace=Path(workspace), provider=None, **kwargs
    )


class TestContracts:
    def test_a_task_that_does_not_write_what_it_promised_fails(self, state, workspace):
        g = graph(task("T-001", writes=["needed"]))
        agents = {"noop": RecordingAgent()}
        engine_for(g, agents, state, workspace).run()

        assert g["T-001"].status is TaskStatus.FAILED
        assert "promised to write" in g["T-001"].error

    def test_a_task_whose_reads_are_absent_never_runs(self, state, workspace):
        g = graph(task("T-001", reads=["missing"]))
        agent = RecordingAgent()
        engine_for(g, {"noop": agent}, state, workspace).run()

        assert g["T-001"].status is TaskStatus.FAILED
        assert "not on the blackboard" in g["T-001"].error
        assert agent.calls == 0, "the agent must not be invoked without its inputs"

    def test_a_missing_agent_is_a_fatal_error_not_a_crash(self, state, workspace):
        g = graph(task("T-001", agent="nobody"))
        result = engine_for(g, {}, state, workspace).run()

        assert g["T-001"].status is TaskStatus.FAILED
        assert result.status is RunStatus.FAILED

    def test_blackboard_writes_are_visible_to_later_tasks(self, state, workspace):
        g = graph(
            task("T-001", agent="writer", writes=["value"]),
            task("T-002", agent="reader", depends_on=["T-001"], reads=["value"]),
        )
        reader = RecordingAgent("reader")
        agents = {"writer": RecordingAgent("writer", writes={"value": 42}), "reader": reader}
        engine_for(g, agents, state, workspace).run()

        assert g["T-002"].status is TaskStatus.SUCCEEDED
        assert reader.calls == 1
        assert state.blackboard.get("value") == 42


class TestFailureHandling:
    def test_a_transient_failure_is_retried_with_the_error_attached(self, state, workspace):
        g = graph(task("T-001"))
        agent = RecordingAgent(error=TransientError("rate limited"))
        engine_for(g, {"noop": agent}, state, workspace).run()

        assert agent.calls == 2
        assert g["T-001"].status is TaskStatus.SUCCEEDED
        retried = [e for e in state.events if e.type is EventType.TASK_RETRIED]
        assert len(retried) == 1

    def test_retries_are_bounded(self, state, workspace):
        g = graph(task("T-001", max_attempts=2))

        class AlwaysFails(Agent):
            name = "noop"
            calls = 0

            def run(self, ctx):
                AlwaysFails.calls += 1
                raise TransientError("still down")

        engine_for(g, {"noop": AlwaysFails()}, state, workspace).run()
        assert AlwaysFails.calls == 2
        assert g["T-001"].status is TaskStatus.FAILED

    def test_a_guardrail_violation_is_never_retried(self, state, workspace):
        g = graph(task("T-001"))

        class WritesForbiddenContent(Agent):
            name = "noop"
            calls = 0

            def run(self, ctx):
                WritesForbiddenContent.calls += 1
                from asep.models import Artifact

                return AgentResult(
                    artifacts=[
                        Artifact(
                            id="a",
                            kind=ArtifactKind.CODE,
                            path="wipe.sh",
                            content="rm -rf /",
                            produced_by=ctx.task.id,
                        )
                    ]
                )

        engine_for(g, {"noop": WritesForbiddenContent()}, state, workspace).run()
        assert WritesForbiddenContent.calls == 1
        assert g["T-001"].status is TaskStatus.FAILED
        assert "forbidden" in g["T-001"].error

    def test_downstream_work_is_skipped_not_attempted(self, state, workspace):
        g = graph(
            task("T-001", writes=["never"]),
            task("T-002", depends_on=["T-001"]),
            task("T-003", depends_on=["T-002"]),
        )
        engine_for(g, {"noop": RecordingAgent()}, state, workspace).run()

        assert g["T-002"].status is TaskStatus.SKIPPED
        assert g["T-003"].status is TaskStatus.SKIPPED

    def test_an_agent_raising_an_unexpected_exception_does_not_kill_the_run(
        self, state, workspace
    ):
        g = graph(task("T-001"), task("T-002"))

        class Explodes(Agent):
            name = "boom"

            def run(self, ctx):
                raise ZeroDivisionError("agent bug")

        agents = {"boom": Explodes(), "noop": RecordingAgent()}
        g = graph(task("T-001", agent="boom"), task("T-002", agent="noop"))
        engine_for(g, agents, state, workspace).run()

        assert g["T-001"].status is TaskStatus.FAILED
        assert g["T-002"].status is TaskStatus.SUCCEEDED, "independent work still runs"


class TestApproval:
    def test_a_rejected_task_stops_everything_behind_it(self, state, workspace):
        g = graph(
            task("T-010", agent="implementation"),
            task("T-020", depends_on=["T-010"]),
        )
        policy = Policy(
            auto_approve=False,
            handler=lambda t, c: ApprovalDecision(
                task_id=t.id, approved=False, approver="reviewer", reason="not this time"
            ),
        )
        agent = RecordingAgent("implementation")
        result = engine_for(
            g, {"implementation": agent, "noop": RecordingAgent()}, state, workspace, policy=policy
        ).run()

        assert agent.calls == 0, "a rejected task must never execute"
        assert g["T-010"].status is TaskStatus.REJECTED
        assert g["T-020"].status is TaskStatus.SKIPPED
        assert result.status is RunStatus.REJECTED_BY_HUMAN

    def test_the_approval_context_reaches_the_reviewer(self, state, workspace):
        seen = {}

        def handler(t, context):
            seen[t.id] = context
            return ApprovalDecision(task_id=t.id, approved=True, approver="reviewer")

        g = graph(
            task(
                "T-010",
                agent="implementation",
                rationale="writes source into the workspace",
                covers=["FR-001"],
            )
        )
        engine_for(
            g,
            {"implementation": RecordingAgent("implementation")},
            state,
            workspace,
            policy=Policy(auto_approve=False, handler=handler),
        ).run()

        assert "writes source into the workspace" in seen["T-010"]
        assert "FR-001" in seen["T-010"]


class TestClarification:
    def test_a_blocking_ambiguity_halts_the_run(self, state, workspace):
        class NeedsAnswer(Agent):
            name = "asker"

            def run(self, ctx):
                raise ClarificationRequired(["AMB-001: what scale?"])

        g = graph(task("T-001", agent="asker"), task("T-002", depends_on=["T-001"]))
        result = engine_for(g, {"asker": NeedsAnswer(), "noop": RecordingAgent()}, state, workspace).run()

        assert result.status is RunStatus.NEEDS_CLARIFICATION
        assert g["T-001"].status is not TaskStatus.RUNNING, "no task is left mid-flight"
        questions = [e for e in state.events if e.type is EventType.CLARIFICATION_REQUESTED]
        assert questions and questions[0].data["questions"] == ["AMB-001: what scale?"]


def _failing_report(repairable: bool = True) -> ValidationReport:
    finding = Finding(
        check="api_contract",
        severity=Severity.ERROR,
        message="declared endpoint has no route",
        target_path="app/api/routes.py",
        repair_hint="add the route" if repairable else None,
    )
    return ValidationReport.from_checks(
        [CheckResult(name="api_contract", status=CheckStatus.FAIL, findings=[finding])]
    )


class TestRepairLoop:
    """The behaviour a fixed pipeline cannot express."""

    def _validation_engine(self, state, workspace, reports, **kwargs):
        sequence = list(reports)

        class Validation(Agent):
            name = "validation"

            def run(self, ctx):
                report = sequence.pop(0) if sequence else ValidationReport.from_checks([])
                return AgentResult(writes={"validation_report": report})

        class Repair(Agent):
            name = "repair"

            def run(self, ctx):
                Repair.findings_seen = list(ctx.findings)
                return AgentResult(writes={"repair_summary": {"summary": "fixed"}})

        Repair.findings_seen = []
        state.blackboard.put("artifacts_index", {})
        state.blackboard.put("api_contract", None)

        g = graph(task("T-040", agent="validation", writes=["validation_report"]))
        engine = engine_for(
            g, {"validation": Validation(), "repair": Repair()}, state, workspace, **kwargs
        )
        return engine, g, Repair

    def test_a_failing_check_injects_repair_and_revalidation(self, state, workspace):
        engine, g, Repair = self._validation_engine(
            state, workspace, [_failing_report(), ValidationReport.from_checks([])]
        )
        engine.run()

        assert "T-900-R1" in g and "T-901-R1" in g
        assert g["T-900-R1"].status is TaskStatus.SUCCEEDED
        assert g["T-901-R1"].status is TaskStatus.SUCCEEDED
        assert engine.repair_rounds == 1

    def test_the_repair_agent_receives_structured_findings(self, state, workspace):
        engine, _, Repair = self._validation_engine(
            state, workspace, [_failing_report(), ValidationReport.from_checks([])]
        )
        engine.run()

        assert len(Repair.findings_seen) == 1
        assert Repair.findings_seen[0].repair_hint == "add the route"

    def test_a_finding_with_no_hint_escalates_instead_of_looping(self, state, workspace):
        engine, g, _ = self._validation_engine(state, workspace, [_failing_report(repairable=False)])
        result = engine.run()

        assert engine.repair_rounds == 0
        assert "T-900-R1" not in g
        assert any(e.type is EventType.ESCALATED for e in state.events)
        assert result.status is RunStatus.FAILED

    def test_the_repair_budget_is_finite(self, state, workspace):
        engine, g, _ = self._validation_engine(
            state, workspace, [_failing_report()] * 6, max_repair_rounds=2
        )
        engine.run()

        assert engine.repair_rounds == 2
        assert "T-900-R3" not in g
        escalations = [e for e in state.events if e.type is EventType.ESCALATED]
        assert any("budget" in e.message for e in escalations)

    def test_tasks_waiting_on_validation_are_moved_behind_the_revalidation(
        self, state, workspace
    ):
        """Otherwise a summary runs while the repair is still rewriting files."""
        sequence = [_failing_report(), ValidationReport.from_checks([])]
        order: list[str] = []

        class Validation(Agent):
            name = "validation"

            def run(self, ctx):
                order.append(ctx.task.id)
                return AgentResult(
                    writes={"validation_report": sequence.pop(0) if sequence else ValidationReport.from_checks([])}
                )

        class Repair(Agent):
            name = "repair"

            def run(self, ctx):
                order.append(ctx.task.id)
                return AgentResult(writes={"repair_summary": {}})

        class Summary(Agent):
            name = "summary"

            def run(self, ctx):
                order.append(ctx.task.id)
                return AgentResult(writes={"run_summary": {}})

        state.blackboard.put("artifacts_index", {})
        state.blackboard.put("api_contract", None)
        g = graph(
            task("T-040", agent="validation", writes=["validation_report"]),
            task("T-050", agent="summary", depends_on=["T-040"], writes=["run_summary"]),
        )
        engine_for(
            g,
            {"validation": Validation(), "repair": Repair(), "summary": Summary()},
            state,
            workspace,
        ).run()

        assert order.index("T-050") > order.index("T-901-R1"), (
            "the summary must observe the repaired state, not the state that failed"
        )
        assert "T-901-R1" in g["T-050"].depends_on


class TestGraphMutation:
    def test_an_agent_can_add_work_to_the_running_graph(self, state, workspace):
        class Planner(Agent):
            name = "planner"

            def run(self, ctx):
                return AgentResult(
                    writes={"plan": True},
                    follow_up_tasks=[task("T-010", agent="noop", depends_on=[ctx.task.id])],
                )

        g = graph(task("T-001", agent="planner", writes=["plan"]))
        engine_for(g, {"planner": Planner(), "noop": RecordingAgent()}, state, workspace).run()

        assert "T-010" in g
        assert g["T-010"].status is TaskStatus.SUCCEEDED
        mutations = [e for e in state.events if e.type is EventType.GRAPH_MUTATED]
        assert any(e.task_id == "T-010" for e in mutations)


class TestConcurrency:
    def test_independent_tasks_are_dispatched_together(self, state, workspace):
        g = graph(
            task("T-001"),
            task("T-002", depends_on=["T-001"]),
            task("T-003", depends_on=["T-001"]),
        )
        engine_for(g, {"noop": RecordingAgent()}, state, workspace).run()

        started = {
            e.task_id: e.data.get("parallel_with", [])
            for e in state.events
            if e.type is EventType.TASK_STARTED
        }
        assert started["T-002"] == ["T-003"]
        assert started["T-003"] == ["T-002"]


class TestTrace:
    def test_every_run_records_a_replayable_trace(self, state, workspace):
        g = graph(task("T-001"))
        engine_for(g, {"noop": RecordingAgent()}, state, workspace).run()

        types = [e.type for e in state.events]
        assert types[0] is EventType.RUN_STARTED
        assert types[-1] is EventType.RUN_FINISHED
        assert [e.seq for e in state.events] == list(range(1, len(state.events) + 1))
