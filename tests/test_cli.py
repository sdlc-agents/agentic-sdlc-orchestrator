"""The command line: exit codes, parsing, and what it prints.

This is the surface a reviewer actually touches, and its exit codes are load
bearing — the documentation and CI both claim a halted run exits non-zero, and
a CI step that silently passes on failure is worse than no CI step.

The observer is tested too. It once crashed a finished run by iterating an
event field that is a count in one event and a list in another; the run had
already succeeded, and the crash happened while reporting it.

Tests here that execute a whole orchestration run are marked `slow`, because
that is what they are — living in the CLI module does not make an end-to-end run
cheap, and mislabelling them quietly turned the fast suite into a 30-second one.
"""

from __future__ import annotations

import json

import pytest

from asep.cli import build_parser, main, make_observer, print_report
from asep.models import Event, EventType, RunStatus
from asep.runner import RunConfig, run


@pytest.fixture
def out(tmp_path):
    return str(tmp_path / "runs")


class TestExitCodes:
    """CI depends on these, so they are asserted rather than assumed."""

    def test_listing_scenarios_succeeds(self, capsys):
        assert main(["--list"]) == 0
        printed = capsys.readouterr().out
        assert "url_shortener" in printed
        assert "fix_expiry_bug" in printed

    @pytest.mark.slow
    def test_a_successful_run_exits_zero(self, out, capsys):
        assert main(["fix_expiry_bug", "--no-tests", "--quiet", "--out", out]) == 0

    def test_a_run_that_will_not_guess_exits_non_zero(self, out, capsys):
        code = main(["url_shortener", "--no-assume", "--quiet", "--out", out])
        assert code == 1, "a halted run must not look like success to CI"

    def test_an_unanswerable_requirement_exits_two(self, out, capsys):
        code = main(
            [
                "url_shortener",
                "--requirement",
                "Build a distributed message broker.",
                "--out",
                out,
            ]
        )
        assert code == 2
        assert "cannot answer a different requirement" in capsys.readouterr().err

    def test_a_missing_seed_codebase_exits_two_rather_than_tracing_back(
        self, out, capsys, monkeypatch
    ):
        import dataclasses

        from asep.scenarios import SCENARIOS

        # Scenario is frozen, so swap the registry entry rather than mutate it.
        broken = dataclasses.replace(
            SCENARIOS["analytics_upgrade"],
            seed_from=SCENARIOS["analytics_upgrade"].seed_from.parent / "gone",
        )
        monkeypatch.setitem(SCENARIOS, "analytics_upgrade", broken)

        assert main(["analytics_upgrade", "--quiet", "--out", out]) == 2
        assert "does not exist" in capsys.readouterr().err


class TestArgumentParsing:
    def test_an_unknown_scenario_is_rejected_by_the_parser(self):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["no_such_scenario"])

    def test_answers_are_parsed_into_pairs(self, out):
        args = build_parser().parse_args(
            ["url_shortener", "--answer", "AMB-001=10k rps", "--answer", "AMB-002=none"]
        )
        assert args.answer == ["AMB-001=10k rps", "AMB-002=none"]

    def test_a_malformed_answer_is_refused_with_an_explanation(self, out):
        with pytest.raises(SystemExit, match="AMB-001=text"):
            main(["url_shortener", "--answer", "no-equals-sign", "--out", out])

    def test_an_answer_containing_equals_signs_is_kept_whole(self, out, capsys):
        """`--answer AMB-001=a=b` must not lose the tail."""
        from asep.cli import _parse_answers

        assert _parse_answers(["AMB-001=ratio=100:1"]) == {"AMB-001": "ratio=100:1"}

    def test_the_threshold_flag_reaches_the_policy(self, out):
        args = build_parser().parse_args(["url_shortener", "--threshold", "high"])
        assert args.threshold == "high"

    def test_defaults_are_the_documented_ones(self):
        args = build_parser().parse_args([])
        assert args.scenario == "url_shortener"
        assert args.mode == "mock"
        assert args.max_repair_rounds == 2
        assert args.approve is False


class TestJsonOutput:
    @pytest.mark.slow
    def test_every_line_is_valid_json_and_the_trace_is_ordered(self, out, capsys):
        assert main(["fix_expiry_bug", "--no-tests", "--json", "--out", out]) == 0

        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
        events = [json.loads(ln) for ln in lines]
        assert events, "no events emitted"

        trace = [e for e in events if "seq" in e]
        assert [e["seq"] for e in trace] == list(range(1, len(trace) + 1))
        assert trace[0]["type"] == EventType.RUN_STARTED.value

    @pytest.mark.slow
    def test_the_last_line_reports_the_outcome_for_piping(self, out, capsys):
        main(["fix_expiry_bug", "--no-tests", "--json", "--out", out])
        last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert last["status"] == "succeeded"
        assert "run_dir" in last


class TestObserver:
    def test_it_survives_an_event_whose_field_is_a_count_not_a_list(self, capsys):
        """run_finished reports an artifact total; task_succeeded a list of paths.

        Iterating the first as if it were the second crashed a finished run.
        """
        observer = make_observer()
        observer(
            Event(
                seq=1,
                type=EventType.RUN_FINISHED,
                message="run succeeded",
                data={"artifacts": 27, "counts": {"succeeded": 11}},
            )
        )
        assert "run succeeded" in capsys.readouterr().out

    def test_it_expands_list_fields_for_the_reader(self, capsys):
        observer = make_observer()
        observer(
            Event(
                seq=1,
                type=EventType.TASK_SUCCEEDED,
                task_id="T-010",
                message="T-010 wrote files",
                data={"artifacts": ["app/main.py", "app/db.py"]},
            )
        )
        printed = capsys.readouterr().out
        assert "app/main.py" in printed and "app/db.py" in printed

    def test_a_long_list_is_truncated_rather_than_flooding_the_terminal(self, capsys):
        observer = make_observer()
        observer(
            Event(
                seq=1,
                type=EventType.TASK_SUCCEEDED,
                message="wrote many",
                data={"artifacts": [f"f{i}.py" for i in range(20)]},
            )
        )
        printed = capsys.readouterr().out
        assert "and 14 more" in printed

    def test_quiet_suppresses_progress_but_never_decisions(self, capsys):
        observer = make_observer(quiet=True)
        observer(Event(seq=1, type=EventType.TASK_STARTED, message="T-001 starting"))
        observer(Event(seq=2, type=EventType.ESCALATED, message="needs a human"))
        printed = capsys.readouterr().out
        assert "starting" not in printed
        assert "needs a human" in printed, "an escalation must never be hidden"

    def test_json_mode_emits_only_json(self, capsys):
        observer = make_observer(as_json=True)
        observer(Event(seq=1, type=EventType.TASK_STARTED, message="T-001"))
        assert json.loads(capsys.readouterr().out.strip())["seq"] == 1


class TestReport:
    @pytest.mark.slow
    def test_a_successful_run_reports_its_evidence(self, tmp_path, capsys):
        result = run(RunConfig(scenario="fix_expiry_bug", out_dir=tmp_path, run_tests=False))
        print_report(result)
        printed = capsys.readouterr().out

        assert "succeeded" in printed
        assert str(result.workspace) in printed
        assert "trace" in printed
        assert "validation:" in printed

    @pytest.mark.slow
    def test_a_halted_run_prints_the_question_and_how_to_answer_it(
        self, tmp_path, capsys
    ):
        result = run(
            RunConfig(scenario="url_shortener", out_dir=tmp_path, assume_defaults=False)
        )
        assert result.state.status is RunStatus.NEEDS_CLARIFICATION
        print_report(result)
        printed = capsys.readouterr().out

        assert "will not guess" in printed
        assert "AMB-001" in printed
        assert "--answer" in printed, "the reader must be told how to unblock it"

    @pytest.mark.slow
    def test_approvals_are_shown_so_the_gate_is_visible(self, tmp_path, capsys):
        result = run(RunConfig(scenario="fix_expiry_bug", out_dir=tmp_path, run_tests=False))
        print_report(result)
        printed = capsys.readouterr().out
        assert "approvals" in printed
        assert "auto-approve" in printed
