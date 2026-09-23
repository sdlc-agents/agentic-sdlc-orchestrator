"""Resuming a halted run.

A run that stops for a human answer used to be unrecoverable: the only way
forward was to re-run everything, which throws away work that was already
approved and paid for. These cover the round trip — halt, answer, continue —
and the rehydration it depends on.
"""

from __future__ import annotations

import pytest

from asep.models import RunStatus, TaskStatus
from asep.orchestration import RunRecorder
from asep.runner import RunConfig, run

pytestmark = pytest.mark.slow


@pytest.fixture
def halted(tmp_path):
    result = run(
        RunConfig(scenario="url_shortener", out_dir=tmp_path, assume_defaults=False)
    )
    assert result.state.status is RunStatus.NEEDS_CLARIFICATION
    return result


class TestRoundTrip:
    def test_answering_lets_the_same_run_continue(self, halted, tmp_path):
        result = run(
            RunConfig(
                scenario="url_shortener",
                out_dir=tmp_path,
                assume_defaults=False,
                resume=halted.state.run_id,
                answers={"AMB-001": "5k redirects/sec over 200k links"},
            )
        )
        assert result.state.status is RunStatus.SUCCEEDED
        assert result.state.run_id == halted.state.run_id, "resume must not fork a new run"

    def test_the_human_answer_survives_into_the_resumed_run(self, halted, tmp_path):
        result = run(
            RunConfig(
                scenario="url_shortener",
                out_dir=tmp_path,
                assume_defaults=False,
                resume=halted.state.run_id,
                answers={"AMB-001": "5k redirects/sec over 200k links"},
            )
        )
        requirement = result.state.blackboard.get("requirement")
        answered = [a for a in requirement.assumptions if a.resolves == "AMB-001"]
        assert answered[0].source == "human"

    def test_one_run_directory_not_two(self, halted, tmp_path):
        run(
            RunConfig(
                scenario="url_shortener",
                out_dir=tmp_path,
                assume_defaults=False,
                resume=halted.state.run_id,
                answers={"AMB-001": "x"},
            )
        )
        assert len(list(tmp_path.iterdir())) == 1


class TestCompletedWorkIsKept:
    @pytest.fixture
    def partial(self, tmp_path):
        """A run that got most of the way, then failed validation."""
        result = run(
            RunConfig(
                scenario="url_shortener", out_dir=tmp_path, max_repair_rounds=0
            )
        )
        assert result.state.status is RunStatus.FAILED
        return result

    def test_succeeded_tasks_are_not_repeated(self, partial, tmp_path):
        before = {
            t.id for t in partial.state.tasks.values() if t.status is TaskStatus.SUCCEEDED
        }
        assert len(before) >= 5

        resumed = run(
            RunConfig(
                scenario="url_shortener",
                out_dir=tmp_path,
                max_repair_rounds=0,
                resume=partial.state.run_id,
            )
        )
        for task_id in before:
            task = resumed.state.tasks[task_id]
            assert task.status is TaskStatus.SUCCEEDED
            assert task.attempts <= 1, f"{task_id} was re-executed on resume"

    def test_the_resume_is_announced_in_the_trace(self, partial, tmp_path):
        resumed = run(
            RunConfig(
                scenario="url_shortener",
                out_dir=tmp_path,
                max_repair_rounds=0,
                resume=partial.state.run_id,
            )
        )
        notices = [e for e in resumed.state.events if e.data.get("resumed")]
        assert notices, "a resumed run should say so"
        assert notices[0].data["already_done"]


class TestRehydration:
    def test_the_blackboard_survives_a_save_and_load(self, tmp_path):
        result = run(
            RunConfig(scenario="fix_expiry_bug", out_dir=tmp_path, run_tests=False)
        )
        reloaded = RunRecorder.load(tmp_path, result.state.run_id)

        original = result.state.blackboard.get("requirement")
        restored = reloaded.blackboard.get("requirement")
        assert restored == original, "models must come back as models, not dicts"
        assert restored.functional[0].id == original.functional[0].id

    def test_a_contract_comes_back_typed(self, tmp_path):
        from asep.models import ApiContract

        result = run(
            RunConfig(scenario="fix_expiry_bug", out_dir=tmp_path, run_tests=False)
        )
        reloaded = RunRecorder.load(tmp_path, result.state.run_id)
        assert isinstance(reloaded.blackboard.get("api_contract"), ApiContract)

    def test_plain_data_survives_too(self, tmp_path):
        result = run(
            RunConfig(scenario="fix_expiry_bug", out_dir=tmp_path, run_tests=False)
        )
        reloaded = RunRecorder.load(tmp_path, result.state.run_id)
        digests = reloaded.blackboard.get("baseline_digests")
        assert isinstance(digests, dict) and digests

    def test_failed_tasks_return_to_pending_so_they_can_be_retried(self, tmp_path):
        result = run(
            RunConfig(scenario="url_shortener", out_dir=tmp_path, max_repair_rounds=0)
        )
        reloaded = RunRecorder.load(tmp_path, result.state.run_id)
        assert not any(
            t.status is TaskStatus.FAILED for t in reloaded.tasks.values()
        )


class TestRefusals:
    def test_resuming_a_run_that_does_not_exist_is_an_error(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            run(
                RunConfig(
                    scenario="url_shortener", out_dir=tmp_path, resume="nosuchrun"
                )
            )

    def test_resuming_under_a_different_scenario_is_refused(self, tmp_path):
        from asep.runner import RequirementNotScripted

        result = run(
            RunConfig(scenario="fix_expiry_bug", out_dir=tmp_path, run_tests=False)
        )
        with pytest.raises(RequirementNotScripted, match="not 'url_shortener'"):
            run(
                RunConfig(
                    scenario="url_shortener",
                    out_dir=tmp_path,
                    resume=result.state.run_id,
                )
            )
