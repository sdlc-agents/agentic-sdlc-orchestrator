"""Every kind of engineering work the platform claims to support.

Greenfield and enhancement are covered in test_scenarios.py. These are the
other four — fix, refactor, test improvement, documentation — plus the property
that separates a well-defined requirement from an ambiguous one.

Each kind gets two assertions that matter: the run produced the right *sort* of
change, and the check that makes that sort of success measurable is not vacuous.
A `behaviour_preserved` that passes no matter what happened would be worse than
not having it, so it is attacked here rather than trusted.
"""

from __future__ import annotations

import shutil

import pytest

from asep.models import CheckStatus, EventType, RunStatus, TaskStatus
from asep.runner import RunConfig, run
from asep.scenarios import SCENARIOS
from asep.tools.workspace import Workspace
from asep.validation import (
    CheckContext,
    check_behaviour_preserved,
    check_documentation,
    check_test_coverage,
)

pytestmark = pytest.mark.slow


def _run(scenario: str, out):
    return run(RunConfig(scenario=scenario, out_dir=out))


@pytest.fixture(scope="module")
def bug_fix(tmp_path_factory):
    return _run("fix_expiry_bug", tmp_path_factory.mktemp("bugfix"))


@pytest.fixture(scope="module")
def refactor(tmp_path_factory):
    return _run("extract_service_layer", tmp_path_factory.mktemp("refactor"))


@pytest.fixture(scope="module")
def coverage(tmp_path_factory):
    return _run("raise_test_coverage", tmp_path_factory.mktemp("coverage"))


@pytest.fixture(scope="module")
def docs(tmp_path_factory):
    return _run("document_the_service", tmp_path_factory.mktemp("docs"))


class TestWorkKindsAreDistinct:
    def test_every_advertised_kind_of_work_has_a_scenario(self):
        kinds = {s.work for s in SCENARIOS.values()}
        assert kinds == {
            "build",
            "enhancement",
            "bug_fix",
            "refactor",
            "test_improvement",
            "documentation",
        }

    def test_the_graph_shape_differs_by_kind(self):
        """A platform that runs the same pipeline for every job supports one job."""
        agents = {
            name: [t.agent for t in s.tasks] for name, s in SCENARIOS.items()
        }
        # A test-improvement change designs nothing, so it has no design stage.
        assert "architecture" not in agents["raise_test_coverage"]
        assert "api_design" not in agents["raise_test_coverage"]
        # A bug fix restates the violated contract but designs no architecture.
        assert "architecture" not in agents["fix_expiry_bug"]
        assert "api_design" in agents["fix_expiry_bug"]
        # Greenfield designs both.
        assert {"architecture", "api_design"} <= set(agents["url_shortener"])

    def test_each_kind_declares_the_checks_that_judge_it(self):
        assert SCENARIOS["extract_service_layer"].checks == ("behaviour_preserved",)
        assert SCENARIOS["raise_test_coverage"].checks == ("test_coverage",)
        assert SCENARIOS["document_the_service"].checks == ("documentation",)


class TestBugFix:
    def test_the_run_succeeds(self, bug_fix):
        assert bug_fix.state.status is RunStatus.SUCCEEDED

    def test_the_defect_was_reproduced_before_it_was_repaired(self, bug_fix):
        """Otherwise a passing suite afterwards is evidence of nothing."""
        order = [
            e.task_id
            for e in bug_fix.state.events
            if e.type is EventType.TASK_STARTED
        ]
        reproduction = next(
            t for t in bug_fix.state.tasks.values() if t.agent == "reproduction"
        )
        fix = next(
            t
            for t in bug_fix.state.tasks.values()
            if t.agent == "implementation"
        )
        assert order.index(reproduction.id) < order.index(fix.id)

        evidence = bug_fix.state.blackboard.get("reproduction")
        assert evidence["reproduced"], "the regression test must fail before the fix"
        assert any("expiry_regression" in name for name in evidence["reproduced"])

    def test_the_same_tests_pass_after_the_fix(self, bug_fix):
        report = bug_fix.state.blackboard.get("final_validation_report")
        tests = next(c for c in report.checks if c.name == "tests")
        assert tests.status is CheckStatus.PASS
        assert "0 failed" in tests.detail

    def test_the_defect_is_actually_gone_from_the_deliverable(self, bug_fix):
        """Checked in the produced code, not inferred from a green run."""
        routes = (bug_fix.workspace / "app" / "api" / "routes.py").read_text(
            encoding="utf-8"
        )
        assert "_entry_expired" in routes
        assert "cache.invalidate(code)" in routes

    def test_the_fix_was_a_surgical_edit_not_a_rewrite(self, bug_fix):
        summary = bug_fix.state.blackboard.get("implementation_summary")
        assert summary["paths"] == ["app/api/routes.py"], (
            "a one-line defect should not touch more than the file that has it"
        )

    def test_the_existing_suite_was_not_edited_to_make_the_fix_pass(self, bug_fix):
        baseline = bug_fix.state.blackboard.get("baseline_digests")
        workspace = Workspace(bug_fix.workspace)
        result = check_behaviour_preserved(
            CheckContext(workspace=workspace, baseline_digests=baseline, run_tests=False)
        )
        assert result.status is CheckStatus.PASS


class TestRefactor:
    def test_the_run_succeeds(self, refactor):
        assert refactor.state.status is RunStatus.SUCCEEDED

    def test_a_service_layer_now_exists_and_the_handlers_delegate_to_it(self, refactor):
        assert (refactor.workspace / "app" / "service.py").exists()
        routes = (refactor.workspace / "app" / "api" / "routes.py").read_text(
            encoding="utf-8"
        )
        assert "app.state.service" in routes
        assert "CodeUnavailable" not in routes, "persistence details should not leak into handlers"

    def test_the_existing_tests_passed_unedited(self, refactor):
        report = refactor.state.blackboard.get("final_validation_report")
        preserved = next(c for c in report.checks if c.name == "behaviour_preserved")
        assert preserved.status is CheckStatus.PASS
        assert "untouched" in preserved.detail

        tests = next(c for c in report.checks if c.name == "tests")
        assert tests.status is CheckStatus.PASS

    def test_no_endpoint_was_lost_in_the_restructuring(self, refactor):
        report = refactor.state.blackboard.get("final_validation_report")
        contract = next(c for c in report.checks if c.name == "api_contract")
        assert contract.status is CheckStatus.PASS
        assert contract.detail.startswith("6/6")

    def test_no_new_tests_were_written(self, refactor):
        """Adding tests during a refactor quietly redefines 'unchanged'."""
        assert refactor.state.blackboard.get("test_suite_index") is None

    def test_the_preservation_check_is_not_vacuous(self, refactor, tmp_path):
        """Edit a pre-existing test file and the check must fail."""
        target = tmp_path / "tampered"
        shutil.copytree(
            refactor.workspace, target, ignore=shutil.ignore_patterns("__pycache__", "*.db")
        )
        workspace = Workspace(target)
        body = workspace.read("tests/test_api.py")
        workspace.write("tests/test_api.py", body + "\n# adjusted to fit the new shape\n")

        result = check_behaviour_preserved(
            CheckContext(
                workspace=workspace,
                baseline_digests=refactor.state.blackboard.get("baseline_digests"),
                run_tests=False,
            )
        )
        assert result.status is CheckStatus.FAIL
        assert any(f.target_path == "tests/test_api.py" for f in result.findings)

    def test_deleting_a_pre_existing_test_is_also_caught(self, refactor, tmp_path):
        target = tmp_path / "deleted"
        shutil.copytree(
            refactor.workspace, target, ignore=shutil.ignore_patterns("__pycache__", "*.db")
        )
        workspace = Workspace(target)
        (workspace.root / "tests" / "test_api.py").unlink()

        result = check_behaviour_preserved(
            CheckContext(
                workspace=workspace,
                baseline_digests=refactor.state.blackboard.get("baseline_digests"),
                run_tests=False,
            )
        )
        assert result.status is CheckStatus.FAIL
        assert any("deleted" in f.message for f in result.findings)


class TestTestImprovement:
    def test_the_run_succeeds(self, coverage):
        assert coverage.state.status is RunStatus.SUCCEEDED

    def test_the_new_tests_exist_and_execute(self, coverage):
        assert (coverage.workspace / "tests" / "test_cache.py").exists()
        assert (coverage.workspace / "tests" / "test_repository.py").exists()

        report = coverage.state.blackboard.get("final_validation_report")
        tests = next(c for c in report.checks if c.name == "tests")
        assert tests.status is CheckStatus.PASS
        assert "51 passed" in tests.detail, "32 existing plus 19 new"

    def test_coverage_measurably_improved(self, coverage):
        """Measured on the same scale before and after, not asserted."""
        before = check_test_coverage(
            CheckContext(
                workspace=Workspace(SCENARIOS["raise_test_coverage"].seed_from),
                run_tests=False,
            )
        )
        after = check_test_coverage(
            CheckContext(workspace=Workspace(coverage.workspace), run_tests=False)
        )
        assert len(after.findings) < len(before.findings)

        closed = {f.target_path for f in before.findings} - {
            f.target_path for f in after.findings
        }
        assert {"app/cache.py", "app/repository.py"} <= closed

    def test_remaining_gaps_are_reported_rather_than_hidden(self, coverage):
        report = coverage.state.blackboard.get("final_validation_report")
        gaps = next(c for c in report.checks if c.name == "test_coverage")
        assert gaps.findings, "a coverage report with no remaining gaps is not credible here"
        assert gaps.status is CheckStatus.PASS, "thin coverage is a warning, not a broken build"

    def test_no_production_code_was_touched(self, coverage):
        """The whole point: this change cannot alter behaviour."""
        assert coverage.state.blackboard.get("artifacts_index") is None
        written = set(coverage.state.blackboard.get("test_suite_index"))
        assert all(path.startswith("tests/") for path in written)


class TestDocumentation:
    def test_the_run_succeeds(self, docs):
        assert docs.state.status is RunStatus.SUCCEEDED

    def test_the_documents_a_maintainer_needs_were_written(self, docs):
        for path in (
            "docs/api-reference.md",
            "docs/runbook.md",
            "docs/adr/001-sqlite-behind-a-repository.md",
            "docs/adr/002-random-base62-codes.md",
        ):
            assert (docs.workspace / path).exists(), path

    def test_every_published_endpoint_is_documented(self, docs):
        report = docs.state.blackboard.get("final_validation_report")
        documentation = next(c for c in report.checks if c.name == "documentation")
        assert documentation.status is CheckStatus.PASS
        assert "0 undocumented" in documentation.detail

    def test_the_documentation_check_is_not_vacuous(self, docs, tmp_path):
        """Remove one endpoint's documentation and the check must fail."""
        target = tmp_path / "thin"
        shutil.copytree(
            docs.workspace, target, ignore=shutil.ignore_patterns("__pycache__", "*.db")
        )
        workspace = Workspace(target)
        for path in workspace.relative_files("**/*.md"):
            body = workspace.read(path)
            workspace.write(path, body.replace("/readyz", "[removed]"))

        from asep.providers.responses import documentation as script

        result = check_documentation(
            CheckContext(
                workspace=workspace,
                api_contract=script.api_contract(),
                run_tests=False,
            )
        )
        assert result.status is CheckStatus.FAIL
        assert any("/readyz" in f.message for f in result.findings)

    def test_known_defects_are_documented_rather_than_omitted(self, docs):
        runbook = (docs.workspace / "docs" / "runbook.md").read_text(encoding="utf-8")
        assert "expire" in runbook.lower()

    def test_no_code_was_changed(self, docs):
        assert docs.state.blackboard.get("artifacts_index") is None


class TestRequirementClarity:
    """Well-defined and ambiguous requirements are both handled, differently."""

    def test_an_ambiguous_requirement_halts_when_told_not_to_guess(self, tmp_path):
        result = run(
            RunConfig(
                scenario="url_shortener", out_dir=tmp_path, assume_defaults=False
            )
        )
        assert result.state.status is RunStatus.NEEDS_CLARIFICATION

    def test_a_well_defined_requirement_runs_straight_through(self, tmp_path):
        """The same strict setting, and no question to ask, so no interruption."""
        result = run(
            RunConfig(
                scenario="fix_expiry_bug",
                out_dir=tmp_path,
                assume_defaults=False,
            )
        )
        assert result.state.status is RunStatus.SUCCEEDED, (
            "a requirement with nothing blocking must not stop for a human"
        )
        requirement = result.state.blackboard.get("requirement")
        assert requirement.blocking_ambiguities == []

    def test_scenarios_agree_with_the_requirements_they_produce(self, tmp_path):
        """The `well_defined` label must match what the requirement stage finds."""
        for name in ("fix_expiry_bug", "url_shortener"):
            scenario = SCENARIOS[name]
            result = run(
                RunConfig(scenario=name, out_dir=tmp_path / name, run_tests=False)
            )
            requirement = result.state.blackboard.get("requirement")
            had_blocking = any(a.blocking for a in requirement.ambiguities)
            assert had_blocking is not scenario.well_defined, (
                f"{name}: scenario says well_defined={scenario.well_defined} but the "
                f"requirement stage found blocking={had_blocking}"
            )

    def test_a_non_blocking_ambiguity_is_still_recorded(self, bug_fix):
        """Well-defined does not mean nothing was assumed."""
        requirement = bug_fix.state.blackboard.get("requirement")
        assert requirement.ambiguities, "a clean requirement can still have open questions"
        assert requirement.assumptions
        assert all(not a.blocking for a in requirement.ambiguities)


class TestReproductionAgent:
    def test_it_refuses_to_proceed_when_the_defect_does_not_reproduce(
        self, tmp_path, bug_fix
    ):
        """A regression test that was green all along proves nothing."""
        from pathlib import Path

        from asep.agents.reproduction import ReproductionAgent
        from asep.models import RunState, Task
        from asep.orchestration.engine import AgentContext
        from asep.orchestration.errors import FatalError

        # The already-fixed workspace: the regression test now passes, so the
        # reproduction stage must refuse rather than wave it through.
        target = tmp_path / "already-fixed"
        shutil.copytree(
            bug_fix.workspace, target, ignore=shutil.ignore_patterns("__pycache__", "*.db")
        )
        state = RunState(run_id="r", requirement="x", scenario="fix_expiry_bug", mode="mock")
        state.blackboard.put(
            "test_suite_index", {"tests/test_expiry_regression.py": {}}
        )
        ctx = AgentContext(
            task=Task(id="T-020", title="t", agent="reproduction"),
            state=state,
            workspace=Path(target),
            provider=None,
        )
        with pytest.raises(FatalError, match="not reproduced"):
            ReproductionAgent(test_timeout_s=120).run(ctx)


def test_all_six_scenarios_are_runnable(bug_fix, refactor, coverage, docs):
    """The two build scenarios are covered in test_scenarios.py."""
    for result in (bug_fix, refactor, coverage, docs):
        assert result.state.status is RunStatus.SUCCEEDED
        assert all(
            t.status is TaskStatus.SUCCEEDED for t in result.state.tasks.values()
        ), result.scenario.name
