"""The things a reviewer is handed, and whether they actually say anything.

Covers the four requirements that are about *output* rather than execution:
system-level codebase reasoning, demonstrated error recovery, a stated
validation approach, and a final summary complete enough to act on.

Each of these is easy to fake — a heading with nothing under it passes a naive
check — so the assertions look at content, not structure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from asep.agents.codebase import classify, derive_data_flows, reconstruct_layers
from asep.models import EventType, RunStatus, TaskStatus
from asep.runner import RunConfig, run
from asep.scenarios import SCENARIOS
from asep.tools import scan
from asep.tools.workspace import Workspace
from asep.validation import CHECK_INTENT, classify_tests, render_approach

pytestmark = pytest.mark.slow

LEGACY = SCENARIOS["analytics_upgrade"].seed_from


@pytest.fixture(scope="module")
def brownfield(tmp_path_factory):
    return run(RunConfig(scenario="analytics_upgrade", out_dir=tmp_path_factory.mktemp("bf")))


class TestCodebaseReasoning:
    """Requirement 3: impacted modules, APIs and data flows; system-level view."""

    def test_layers_are_reconstructed_from_the_real_tree(self):
        layers = reconstruct_layers(scan(LEGACY))
        assert layers["transport"] == ["app/api/__init__.py", "app/api/routes.py"]
        assert layers["persistence"] == ["app/repository.py"]
        assert layers["storage"] == ["app/db.py"]

    def test_tests_are_not_mistaken_for_the_layer_they_exercise(self):
        assert classify("tests/test_api.py") == "tests"
        assert classify("app/api/routes.py") == "transport"

    def test_data_flows_are_derived_from_the_import_graph(self):
        flows = {(f.source, f.target) for f in derive_data_flows(scan(LEGACY))}
        # These edges exist in the source; none of them is asserted by a model.
        assert ("app/api/routes.py", "app/repository.py") in flows
        assert ("app/repository.py", "app/db.py") in flows
        assert ("app/main.py", "app/cache.py") in flows

    def test_flows_within_one_layer_are_not_reported_as_data_flows(self):
        for flow in derive_data_flows(scan(LEGACY)):
            assert classify(flow.source) != classify(flow.target)

    def test_a_run_identifies_which_flows_pass_through_changed_code(self, brownfield):
        analysis = brownfield.state.blackboard.get("impact_analysis")
        assert analysis.data_flows, "no data flows recovered"
        touched = analysis.flows_through_impacted_code
        assert touched
        assert len(touched) < len(analysis.data_flows) or len(analysis.data_flows) == len(
            touched
        )
        assert any(
            f.source == "app/api/routes.py" and f.target == "app/repository.py"
            for f in touched
        )

    def test_a_run_names_the_published_apis_the_change_reaches(self, brownfield):
        analysis = brownfield.state.blackboard.get("impact_analysis")
        assert analysis.impacted_apis
        assert any("GET /{code}" in api for api in analysis.impacted_apis), (
            "the redirect is served by a file this change modifies"
        )
        assert analysis.entry_points == ["app/api/routes.py"]

    def test_the_report_shows_the_architecture_not_just_a_file_list(self, brownfield):
        report = (brownfield.workspace / "docs" / "impact-analysis.md").read_text(
            encoding="utf-8"
        )
        assert "## The system as it stands" in report
        assert "## Data flows" in report
        assert "## APIs affected" in report
        assert "transport -> persistence" in report


class TestErrorRecovery:
    """Requirement 4: error handling and recovery, demonstrated rather than claimed."""

    def test_an_injected_failure_is_retried_and_the_run_still_succeeds(self, tmp_path):
        result = run(
            RunConfig(
                scenario="url_shortener",
                out_dir=tmp_path,
                run_tests=False,
                inject_failures={"architecture", "implementation"},
            )
        )
        assert result.state.status is RunStatus.SUCCEEDED

        retried = [e for e in result.state.events if e.type is EventType.TASK_RETRIED]
        assert len(retried) == 2, "both injected failures should have been retried"
        assert all("TransientError" in e.message for e in retried)

        # Recovered, not merely retried.
        for task in result.state.tasks.values():
            if task.agent in {"architecture", "implementation"}:
                assert task.status is TaskStatus.SUCCEEDED
                assert task.attempts == 2

    def test_the_retry_is_informed_by_the_failure(self, tmp_path):
        """A second identical attempt is not recovery, it is a coin flip."""
        result = run(
            RunConfig(
                scenario="url_shortener",
                out_dir=tmp_path,
                run_tests=False,
                inject_failures={"architecture"},
            )
        )
        retried = next(e for e in result.state.events if e.type is EventType.TASK_RETRIED)
        assert "error context attached" in retried.message

    def test_injection_is_refused_for_a_real_provider(self):
        """It is a test affordance, not a way to sabotage a live run."""
        from asep.providers import build_provider

        with pytest.raises(ValueError, match="deterministic provider"):
            build_provider("openai", fail_once={"implementation"})


class TestValidationApproach:
    """Requirement 6: a stated validation approach, test strategy and steps."""

    def test_the_run_publishes_an_approach_document(self, brownfield):
        assert (brownfield.run_dir / "validation-approach.md").exists()

    def test_it_is_written_once_not_per_round(self, brownfield):
        approaches = [
            a
            for a in brownfield.state.artifacts.values()
            if a.path == "validation-approach.md"
        ]
        assert len(approaches) == 1

    def test_the_test_strategy_distinguishes_unit_from_integration(self, brownfield):
        tests = classify_tests(Workspace(brownfield.workspace))
        kinds = {t.kind for t in tests}
        assert kinds == {"unit", "integration"}, (
            "both layers must be present and told apart"
        )
        assert all(t.cases > 0 for t in tests)

    def test_every_check_that_ran_states_what_it_cannot_catch(self, brownfield):
        report = brownfield.state.blackboard.get("final_validation_report")
        document = (brownfield.run_dir / "validation-approach.md").read_text(
            encoding="utf-8"
        )
        for check in report.checks:
            assert check.name in document
            assert check.name in CHECK_INTENT, f"{check.name} has no documented blind spot"

    def test_it_states_what_is_not_verified(self, brownfield):
        document = (brownfield.run_dir / "validation-approach.md").read_text(
            encoding="utf-8"
        )
        assert "## What is not verified" in document
        for topic in ("Performance", "Security"):
            assert topic in document

    def test_a_suite_with_no_unit_tests_is_called_out(self, tmp_path):
        workspace = Workspace(tmp_path / "ws")
        workspace.write(
            "tests/test_api.py",
            "def test_a(client):\n    assert client\n",
        )
        document = render_approach(workspace, ["tests"])
        assert "No unit tests" in document

    def test_a_run_with_no_tests_at_all_says_so_plainly(self, tmp_path):
        workspace = Workspace(tmp_path / "ws")
        workspace.write("app/main.py", "x = 1\n")
        document = render_approach(workspace, ["structure"])
        assert "No tests were produced" in document


class TestFinalSummary:
    """Requirement 8: plan, artifacts, risks, validation, assumptions, limitations."""

    @pytest.fixture(scope="class")
    def summary(self, brownfield):
        return (brownfield.run_dir / "summary.md").read_text(encoding="utf-8")

    def test_it_contains_every_required_section(self, summary):
        for heading in (
            "## Implementation plan and rationale",
            "### Generated artifacts",
            "## Decisions and what they cost",
            "## Open risks",
            "## Verification",
            "## Assumptions this run made",
            "## Limitations",
        ):
            assert heading in summary, heading

    def test_the_plan_explains_itself_rather_than_listing_steps(self, summary):
        plan_section = summary.split("## Implementation plan")[1].split("## Execution")[0]
        assert len(plan_section) > 400, "a rationale that short is a label, not a reason"
        assert "Why it is in the plan" in plan_section
        # Injected repair nodes are distinguishable from planned work.
        assert "T-900-R1" in plan_section

    def test_the_artifact_inventory_lists_real_files(self, brownfield, summary):
        inventory = summary.split("### Generated artifacts")[1].split("##")[0]
        for artifact in brownfield.state.artifacts.values():
            if artifact.kind.value != "report":
                assert artifact.path in inventory, artifact.path

    def test_limitations_are_specific_to_this_run(self, brownfield, summary):
        limits = summary.split("## Limitations")[1]
        report = brownfield.state.blackboard.get("final_validation_report")
        tests = next(c for c in report.checks if c.name == "tests")
        assert tests.detail in limits, "the limitation should cite the actual result"
        assert "not been deployed" in limits

    def test_limitations_and_assumptions_are_distinct(self, brownfield):
        run_summary = brownfield.state.blackboard.get("run_summary")
        assert run_summary.limitations
        assert run_summary.next_steps
        assert set(run_summary.limitations) != set(run_summary.next_steps)

    def test_it_points_at_the_validation_approach(self, summary):
        assert "validation-approach.md" in summary

    def test_a_failed_run_says_so_in_its_limitations(self, tmp_path):
        """The section must not read the same way whatever happened."""
        result = run(
            RunConfig(
                scenario="url_shortener",
                out_dir=tmp_path,
                run_tests=False,
                max_repair_rounds=0,
            )
        )
        limits = result.state.blackboard.get("run_summary").limitations
        assert any("did not pass" in item or "escalated" in item for item in limits)


class TestAcceptsARequirement:
    """Requirement 1: it must accept a requirement, or say it cannot."""

    def test_the_scripted_requirement_is_accepted(self, tmp_path):
        from asep.scenarios import SCENARIOS as ALL

        result = run(
            RunConfig(
                scenario="url_shortener",
                requirement=ALL["url_shortener"].requirement,
                out_dir=tmp_path,
                run_tests=False,
            )
        )
        assert result.state.status is RunStatus.SUCCEEDED

    def test_an_unscripted_requirement_is_refused_not_answered_wrongly(self, tmp_path):
        """The deterministic provider would otherwise quote the requirement back
        and return the scripted design anyway — a confident, unrelated answer."""
        from asep.runner import RequirementNotScripted

        with pytest.raises(RequirementNotScripted, match="cannot answer"):
            run(
                RunConfig(
                    scenario="url_shortener",
                    requirement="Build a real-time chat system with presence.",
                    out_dir=tmp_path,
                    run_tests=False,
                )
            )

    def test_the_refusal_names_the_option_that_would_work(self, tmp_path):
        from asep.runner import RequirementNotScripted

        with pytest.raises(RequirementNotScripted) as exc:
            run(
                RunConfig(
                    scenario="url_shortener",
                    requirement="Something else entirely.",
                    out_dir=tmp_path,
                )
            )
        assert "--mode openai" in str(exc.value)

    def test_nothing_is_generated_before_the_refusal(self, tmp_path):
        from asep.runner import RequirementNotScripted

        with pytest.raises(RequirementNotScripted):
            run(
                RunConfig(
                    scenario="url_shortener",
                    requirement="Something else entirely.",
                    out_dir=tmp_path,
                )
            )
        assert list(tmp_path.iterdir()) == [], "refused before any work started"


class TestCommittedExamples:
    """Requirement 3: sample inputs and outputs for all three input kinds."""

    ROOT = Path(__file__).resolve().parents[1] / "examples"

    @pytest.mark.parametrize("name", ["greenfield", "brownfield", "ambiguous"])
    def test_the_example_exists_and_records_its_command(self, name):
        console = self.ROOT / name / "console.txt"
        assert console.exists(), f"examples/{name}/ is missing"
        assert console.read_text(encoding="utf-8").startswith("$ python -m asep")

    @pytest.mark.parametrize("name", ["greenfield", "brownfield"])
    def test_successful_examples_carry_their_validation_evidence(self, name):
        folder = self.ROOT / name
        assert (folder / "summary.md").exists()
        assert (folder / "validation-approach.md").exists()
        assert (folder / "validation-round-1.md").exists()
        # Round 1 failed and round 2 passed: the repair loop is visible.
        assert (folder / "validation-round-2.md").exists()
        assert "FAIL" in (folder / "validation-round-1.md").read_text(encoding="utf-8")
        assert "PASS" in (folder / "validation-round-2.md").read_text(encoding="utf-8")

    def test_the_ambiguous_example_halted_without_generating_code(self):
        import json

        record = json.loads((self.ROOT / "ambiguous" / "run.json").read_text(encoding="utf-8"))
        assert record["status"] == "needs_clarification"
        assert record["artifacts"] == {}, "it must not produce code it could not justify"
        assert not any(
            t["status"] == "running" for t in record["tasks"].values()
        ), "no task left in flight"

    @pytest.mark.parametrize("name", ["greenfield", "brownfield", "ambiguous"])
    def test_each_example_has_a_replayable_trace(self, name):
        import json

        lines = (self.ROOT / name / "trace.jsonl").read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in lines]
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        assert events[0]["type"] == "run_started"
        assert events[-1]["type"] == "run_finished"

    def test_the_examples_demonstrate_decomposition_and_orchestration(self):
        """Each example must show the graph growing, not a fixed sequence."""
        import json

        for name in ("greenfield", "brownfield"):
            lines = (self.ROOT / name / "trace.jsonl").read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in lines]
            injected = [e for e in events if e["type"] == "graph_mutated"]
            assert injected, f"{name}: no tasks injected at runtime"
            concurrent = [
                e
                for e in events
                if e["type"] == "task_started" and e["data"].get("parallel_with")
            ]
            assert concurrent, f"{name}: nothing ran concurrently"


class TestWalkthrough:
    """The walkthrough is the assessor's entry point; its claims must be true.

    A document that cites figures which have since drifted is worse than no
    document, so the load-bearing ones are pinned here.
    """

    ROOT = Path(__file__).resolve().parents[1]

    @pytest.fixture(scope="class")
    def text(self):
        return (self.ROOT / "docs" / "walkthrough.md").read_text(encoding="utf-8")

    def test_it_exists_and_is_linked_from_the_readme(self, text):
        assert text
        readme = (self.ROOT / "README.md").read_text(encoding="utf-8")
        assert "docs/walkthrough.md" in readme

    def test_it_covers_every_stage_the_brief_asks_about(self, text):
        for stage in (
            "Decomposed into an engineering problem",
            "Designed, two stages running concurrently",
            "Decomposed into executable graph nodes",
            "Built, under a human gate",
            "Validated by something that did not write it",
            "Repaired, then re-validated",
            "Reported for a reviewer",
        ):
            assert stage in text, stage

    def test_the_figures_it_quotes_match_the_committed_run(self, text):
        import json

        metrics = json.loads(
            (self.ROOT / "examples" / "greenfield" / "metrics.json").read_text(
                encoding="utf-8"
            )
        )
        assert f"| {metrics['tasks']['planned']} / {metrics['tasks']['injected_at_runtime']} |" in text
        assert (
            f"| {metrics['oversight']['approvals_requested']} / "
            f"{metrics['oversight']['approvals_denied']} |" in text
        )
        assert f"| {metrics['output']['artifacts']} /" in text

    def test_every_artifact_it_links_to_is_committed(self, text):
        import re

        for rel in re.findall(r"\]\(\.\./(examples/[^)]+)\)", text):
            assert (self.ROOT / rel).exists(), f"walkthrough links a missing file: {rel}"
