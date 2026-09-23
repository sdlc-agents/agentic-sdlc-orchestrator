"""End-to-end runs.

These are slow because they are real: the generated suite is actually executed
in a subprocess, which is the only way the claim "the tests pass" means
anything. They assert on the shape of the run — what was gated, what failed
first, what repaired it — rather than on generated prose.
"""

from __future__ import annotations

import json

import pytest

from asep.models import CheckStatus, EventType, RunStatus, TaskStatus
from asep.runner import RunConfig, run

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def greenfield(tmp_path_factory):
    out = tmp_path_factory.mktemp("greenfield")
    return run(RunConfig(scenario="url_shortener", out_dir=out))


@pytest.fixture(scope="module")
def brownfield(tmp_path_factory):
    out = tmp_path_factory.mktemp("brownfield")
    return run(RunConfig(scenario="analytics_upgrade", out_dir=out))


class TestGreenfield:
    def test_the_run_succeeds(self, greenfield):
        assert greenfield.state.status is RunStatus.SUCCEEDED

    def test_the_generated_suite_actually_executed_and_passed(self, greenfield):
        report = greenfield.state.blackboard.get("final_validation_report")
        tests = next(c for c in report.checks if c.name == "tests")
        assert tests.status is CheckStatus.PASS
        assert "passed" in tests.detail and "0 failed" in tests.detail

    def test_the_deliverable_runs_on_its_own(self, greenfield):
        """The workspace is a project, not a folder of snippets."""
        files = set(
            p.relative_to(greenfield.workspace).as_posix()
            for p in greenfield.workspace.rglob("*")
            if p.is_file()
        )
        assert {"app/main.py", "requirements.txt", "README.md", "openapi.yaml"} <= files
        assert any(f.startswith("tests/") for f in files)
        assert any(f.startswith("migrations/") for f in files)

    def test_the_first_validation_round_found_the_missing_endpoint(self, greenfield):
        failures = [
            e for e in greenfield.state.events if e.type is EventType.VALIDATION_FAILED
        ]
        assert failures, "the seeded defect must be detected, not silently absent"
        assert any("analytics" in f for f in failures[0].data["findings"])

    def test_the_repair_loop_closed_it(self, greenfield):
        repairs = [e for e in greenfield.state.events if e.type is EventType.REPAIR_SCHEDULED]
        assert len(repairs) == 1
        assert greenfield.state.tasks["T-900-R1"].status is TaskStatus.SUCCEEDED
        assert greenfield.state.tasks["T-901-R1"].status is TaskStatus.SUCCEEDED

        report = greenfield.state.blackboard.get("final_validation_report")
        assert report.status is CheckStatus.PASS
        assert report.attempt == 2, "the fix is not trusted until it is re-checked"

    def test_writing_code_was_gated_for_a_human(self, greenfield):
        gated = {d.task_id for d in greenfield.state.approvals}
        assert "T-010" in gated, "implementation must not run unattended"
        assert "T-900-R1" in gated, "neither may a repair"

    def test_every_contract_endpoint_exists_in_the_code(self, greenfield):
        report = greenfield.state.blackboard.get("final_validation_report")
        contract_check = next(c for c in report.checks if c.name == "api_contract")
        assert contract_check.status is CheckStatus.PASS
        assert contract_check.detail.startswith("7/7")

    def test_the_planner_grew_the_graph_at_runtime(self, greenfield):
        planned = {t.id for t in greenfield.scenario.tasks}
        executed = set(greenfield.state.tasks)
        assert executed > planned, "the graph must not be fixed up front"
        assert {"T-010", "T-020", "T-030", "T-040", "T-050"} <= executed

    def test_the_trace_is_complete_and_replayable(self, greenfield):
        lines = (greenfield.run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines]
        assert len(events) == len(greenfield.state.events)
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        assert events[-1]["type"] == EventType.RUN_FINISHED.value

    def test_the_run_report_describes_a_finished_run(self, greenfield):
        """The summary task runs inside the run, so it must be re-rendered after."""
        summary = (greenfield.run_dir / "summary.md").read_text(encoding="utf-8")

        # The status line itself, not a substring search: prose elsewhere in the
        # report legitimately contains the word "running".
        status_line = next(
            line for line in summary.splitlines() if line.startswith("**Run ")
        )
        assert "succeeded" in status_line
        assert "running" not in status_line

        # No task may still be reported as in flight.
        task_rows = [line for line in summary.splitlines() if line.startswith("| T-")]
        assert task_rows
        assert not any("| running |" in row for row in task_rows)

        assert "## Repairs" in summary
        assert "## Assumptions this run made" in summary


class TestBrownfield:
    def test_the_run_succeeds(self, brownfield):
        assert brownfield.state.status is RunStatus.SUCCEEDED

    def test_the_existing_suite_still_passes(self, brownfield):
        """The constraint the change is measured against."""
        report = brownfield.state.blackboard.get("final_validation_report")
        tests = next(c for c in report.checks if c.name == "tests")
        assert tests.status is CheckStatus.PASS
        assert "0 failed" in tests.detail

    def test_impact_analysis_ran_before_anything_was_designed(self, brownfield):
        tasks = brownfield.state.tasks
        assert tasks["T-002"].agent == "codebase"
        assert "T-002" in tasks["T-003"].depends_on

    def test_impact_analysis_is_derived_from_the_real_codebase(self, brownfield):
        analysis = brownfield.state.blackboard.get("impact_analysis")
        assert analysis.modules_scanned > 10
        touched = {e.path for e in analysis.impacted}
        assert "app/api/routes.py" in touched
        assert "app/analytics.py" in touched

        # The dependency edges must be real, not empty placeholders.
        referenced = {e.path: e.referenced_by for e in analysis.impacted}
        assert referenced["app/repository.py"], "importers should be resolved from the AST"
        assert "app/main.py" in referenced["app/repository.py"]

    def test_the_regression_surface_names_the_existing_tests(self, brownfield):
        analysis = brownfield.state.blackboard.get("impact_analysis")
        assert any("test_api" in p for p in analysis.regression_surface)

    def test_the_change_is_additive_to_the_schema(self, brownfield):
        migrations = sorted(
            p.name for p in (brownfield.workspace / "migrations").iterdir() if p.is_file()
        )
        assert migrations == ["001_init.sql", "002_add_click_events.sql"]
        added = (brownfield.workspace / "migrations" / "002_add_click_events.sql").read_text(
            encoding="utf-8"
        )
        assert "CREATE TABLE IF NOT EXISTS click_events" in added

    def test_the_original_codebase_was_not_modified(self, brownfield):
        """Agents operate on a copy; the target repository is never touched."""
        source = brownfield.scenario.seed_from
        assert not (source / "app" / "analytics.py").exists()
        assert "analytics" not in (source / "app" / "main.py").read_text(encoding="utf-8")


class TestClarification:
    def test_the_run_refuses_to_guess_when_told_not_to(self, tmp_path):
        result = run(
            RunConfig(scenario="url_shortener", out_dir=tmp_path, assume_defaults=False)
        )
        assert result.state.status is RunStatus.NEEDS_CLARIFICATION
        assert result.ok is False

        questions = [
            e for e in result.state.events if e.type is EventType.CLARIFICATION_REQUESTED
        ]
        assert questions and "AMB-001" in questions[0].data["questions"][0]
        assert not any(
            t.status is TaskStatus.RUNNING for t in result.state.tasks.values()
        ), "no task may be left recorded as in-flight after the process stops"

    def test_answering_the_question_unblocks_the_run(self, tmp_path):
        result = run(
            RunConfig(
                scenario="url_shortener",
                out_dir=tmp_path,
                assume_defaults=False,
                answers={"AMB-001": "5k redirects/sec over 200k links"},
                run_tests=False,
            )
        )
        assert result.state.status is not RunStatus.NEEDS_CLARIFICATION
        requirement = result.state.blackboard.get("requirement")
        answered = [a for a in requirement.assumptions if a.resolves == "AMB-001"]
        assert answered[0].source == "human"
        assert answered[0].statement == "5k redirects/sec over 200k links"


class TestHumanRejection:
    def test_rejecting_the_implementation_stops_the_run(self, tmp_path):
        from asep.models import ApprovalDecision
        from asep.orchestration import Policy

        config = RunConfig(scenario="url_shortener", out_dir=tmp_path, run_tests=False)

        # Drive the engine directly so the rejection is deterministic rather
        # than dependent on an interactive prompt.
        from asep.models import RunState
        from asep.providers import build_provider
        from asep.runner import build_engine, prepare_workspace
        from asep.scenarios import get as get_scenario

        scenario = get_scenario(config.scenario)
        state = RunState(
            run_id="reject", requirement=scenario.requirement, scenario=scenario.name, mode="mock"
        )
        workspace = prepare_workspace(tmp_path / "ws", scenario)
        engine = build_engine(config, scenario, state, workspace, build_provider("mock"))
        engine.policy = Policy(
            auto_approve=False,
            handler=lambda t, c: ApprovalDecision(
                task_id=t.id, approved=False, approver="reviewer", reason="not yet"
            ),
        )
        result = engine.run()

        assert result.status is RunStatus.REJECTED_BY_HUMAN
        assert state.tasks["T-010"].status is TaskStatus.REJECTED
        assert not (workspace.root / "app" / "main.py").exists(), (
            "a rejected implementation must leave nothing behind"
        )
