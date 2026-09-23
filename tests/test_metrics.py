"""Run metrics.

Derived from the trace rather than counted by hand, so these mostly check that
the derivation agrees with what the run actually did. A metric that disagrees
with its own trace is worse than no metric: it gets trusted.
"""

from __future__ import annotations

import json

import pytest

from asep.metrics import collect, render
from asep.models import EventType
from asep.runner import RunConfig, run

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def greenfield(tmp_path_factory):
    return run(RunConfig(scenario="url_shortener", out_dir=tmp_path_factory.mktemp("m")))


class TestAgreesWithTheRun:
    def test_task_counts_match_the_run_state(self, greenfield):
        m = collect(greenfield.state)
        tasks = greenfield.state.tasks
        assert m.tasks_planned + m.tasks_injected == len(tasks)
        assert m.tasks_succeeded == sum(
            1 for t in tasks.values() if t.status.value == "succeeded"
        )

    def test_injected_tasks_are_counted_separately_from_planned(self, greenfield):
        m = collect(greenfield.state)
        assert m.tasks_injected > 0, "the repair loop injects nodes; that should show"
        assert m.tasks_planned > m.tasks_injected

    def test_repair_rounds_match_the_trace(self, greenfield):
        m = collect(greenfield.state)
        scheduled = [
            e for e in greenfield.state.events if e.type is EventType.REPAIR_SCHEDULED
        ]
        assert m.repair_rounds == len(scheduled)

    def test_approvals_match_the_trace(self, greenfield):
        m = collect(greenfield.state)
        requested = [
            e for e in greenfield.state.events if e.type is EventType.APPROVAL_REQUESTED
        ]
        assert m.approvals_requested == len(requested)
        assert m.approvals_denied == 0

    def test_per_agent_time_sums_to_the_agent_tasks(self, greenfield):
        m = collect(greenfield.state)
        for name, agent in m.agents.items():
            tasks = [t for t in greenfield.state.tasks.values() if t.agent == name]
            assert agent.invocations == len(tasks)
            assert agent.total_ms == sum(t.duration_ms for t in tasks)

    def test_validation_figures_come_from_the_final_report(self, greenfield):
        m = collect(greenfield.state)
        report = greenfield.state.blackboard.get("final_validation_report")
        assert m.checks_run == len(report.checks)
        assert m.validation_rounds == report.attempt


class TestFirstPassYield:
    def test_a_run_that_needed_a_repair_is_not_first_pass(self, greenfield):
        """The shipped greenfield run repairs itself, so this must read false."""
        m = collect(greenfield.state)
        assert m.repair_rounds == 1
        assert m.first_pass_yield is False

    def test_a_run_that_needed_no_repair_is_first_pass(self, tmp_path):
        result = run(RunConfig(scenario="fix_expiry_bug", out_dir=tmp_path))
        m = collect(result.state)
        assert m.repair_rounds == 0
        assert m.first_pass_yield is True

    def test_a_failed_run_is_never_first_pass(self, tmp_path):
        result = run(
            RunConfig(scenario="url_shortener", out_dir=tmp_path, max_repair_rounds=0)
        )
        assert result.state.status.value != "succeeded"
        assert collect(result.state).first_pass_yield is False


class TestReliabilitySignals:
    def test_retries_are_counted(self, tmp_path):
        result = run(
            RunConfig(
                scenario="url_shortener",
                out_dir=tmp_path,
                run_tests=False,
                inject_failures={"architecture", "implementation"},
            )
        )
        m = collect(result.state)
        assert m.retries == 2
        assert m.agents["architecture"].retries == 1

    def test_escalations_are_counted(self, tmp_path):
        result = run(
            RunConfig(scenario="url_shortener", out_dir=tmp_path, max_repair_rounds=0)
        )
        assert collect(result.state).escalations >= 1


class TestArtifacts:
    def test_metrics_are_written_beside_the_trace(self, greenfield):
        assert (greenfield.run_dir / "metrics.json").exists()
        assert (greenfield.run_dir / "metrics.md").exists()

    def test_the_json_is_machine_readable_and_complete(self, greenfield):
        data = json.loads((greenfield.run_dir / "metrics.json").read_text(encoding="utf-8"))
        for key in ("tasks", "reliability", "oversight", "validation", "output", "agents"):
            assert key in data, key
        assert data["status"] == "succeeded"
        assert data["reliability"]["repair_rounds"] == 1

    def test_the_summary_names_the_slowest_agents(self, greenfield):
        text = render(collect(greenfield.state))
        assert "Where the time went" in text
        # Validation executes the generated suite, so it dominates.
        assert "`validation`" in text

    def test_the_summary_explains_the_signal_worth_watching(self, greenfield):
        text = render(collect(greenfield.state))
        assert "first-pass yield" in text
