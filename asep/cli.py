"""Command line entry point.

The console output prints decisions — what was scheduled, what ran
concurrently, what needed approval, what was repaired — rather than file
contents.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import Event, EventType, RiskLevel, RunStatus
from .runner import RequirementNotScripted, RunConfig, RunResult, run
from .scenarios import SCENARIOS

# Event -> the two-character gutter mark it prints under.
MARKS = {
    EventType.RUN_STARTED: "**",
    EventType.RUN_FINISHED: "**",
    EventType.TASK_STARTED: ">",
    EventType.TASK_SUCCEEDED: "ok",
    EventType.TASK_FAILED: "XX",
    EventType.TASK_RETRIED: "..",
    EventType.TASK_SKIPPED: "--",
    EventType.APPROVAL_REQUESTED: "??",
    EventType.APPROVAL_GRANTED: "ok",
    EventType.APPROVAL_DENIED: "XX",
    EventType.VALIDATION_PASSED: "ok",
    EventType.VALIDATION_FAILED: "!!",
    EventType.REPAIR_SCHEDULED: "++",
    EventType.GRAPH_MUTATED: "++",
    EventType.ESCALATED: "!!",
    EventType.CLARIFICATION_REQUESTED: "??",
}

QUIET_TYPES = {EventType.TASK_STARTED, EventType.GRAPH_MUTATED}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asep",
        description=(
            "Run a software engineering task through an orchestrated agent graph."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_scenario_help(),
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        default="url_shortener",
        choices=sorted(SCENARIOS),
        help="which scenario to run (default: url_shortener)",
    )
    parser.add_argument(
        "--mode",
        default="mock",
        choices=("mock", "openai"),
        help="mock is deterministic and needs no API key (default: mock)",
    )
    parser.add_argument("--model", default=None, help="model id, for --mode openai")
    parser.add_argument(
        "--requirement",
        default=None,
        help=(
            "override the scenario's requirement text; needs --mode openai, since "
            "the deterministic provider only knows the scripted requirement"
        ),
    )
    parser.add_argument("--out", default="runs", help="where run records are written")
    parser.add_argument(
        "--workspace", default=None, help="where generated files land (default: inside the run dir)"
    )

    approval = parser.add_argument_group("human in the loop")
    approval.add_argument(
        "--approve",
        action="store_true",
        help="stop for an interactive decision on every gated task",
    )
    approval.add_argument(
        "--threshold",
        default="medium",
        choices=("low", "medium", "high"),
        help="risk level at or above which a task needs approval (default: medium)",
    )
    approval.add_argument(
        "--no-assume",
        action="store_true",
        help="do not apply default assumptions; halt on any blocking ambiguity",
    )
    approval.add_argument(
        "--resume",
        default=None,
        metavar="RUN-ID",
        help=(
            "continue a halted run instead of starting over; work that already "
            "succeeded is kept"
        ),
    )
    approval.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="AMB-001=answer",
        help="answer an ambiguity up front; repeatable",
    )

    execution = parser.add_argument_group("execution")
    execution.add_argument(
        "--no-tests", action="store_true", help="skip executing the generated suite"
    )
    execution.add_argument(
        "--test-timeout", type=int, default=180, help="seconds before the suite is killed"
    )
    execution.add_argument(
        "--max-repair-rounds",
        type=int,
        default=2,
        help="how many times validation may schedule a repair (default: 2)",
    )
    execution.add_argument(
        "--max-workers", type=int, default=4, help="concurrent task limit (default: 4)"
    )
    execution.add_argument(
        "--inject-failure",
        action="append",
        default=[],
        metavar="AGENT",
        help=(
            "make an agent fail on its first attempt, to watch retry and recovery "
            "happen in a real run; repeatable (mock mode only)"
        ),
    )

    output = parser.add_argument_group("output")
    output.add_argument("--quiet", action="store_true", help="only decisions and outcomes")
    output.add_argument("--json", action="store_true", help="emit the trace as JSON lines")
    output.add_argument(
        "--list", action="store_true", help="list the available scenarios and exit"
    )
    return parser


def _scenario_help() -> str:
    lines = ["scenarios:"]
    for scenario in SCENARIOS.values():
        lines += [
            f"  {scenario.name:<18} {scenario.title}",
            f"  {'':<18} {scenario.description}",
            f"  {'':<18} \"{scenario.requirement}\"",
            "",
        ]
    return "\n".join(lines)


def _parse_answers(pairs: list[str]) -> dict[str, str]:
    answers = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--answer expects AMB-001=text, got: {pair!r}")
        key, value = pair.split("=", 1)
        answers[key.strip()] = value.strip()
    return answers


def make_observer(quiet: bool = False, as_json: bool = False):
    def observer(event: Event) -> None:
        if as_json:
            print(event.model_dump_json())
            return
        if quiet and event.type in QUIET_TYPES:
            return

        mark = MARKS.get(event.type, "  ")
        print(f"  {mark:>2}  {event.message}")

        for key in ("parallel_with", "wrote", "artifacts", "findings", "questions"):
            value = event.data.get(key)
            # Some events carry a count under the same key (run_finished reports
            # an artifact total, task_succeeded a list of paths).
            if not value or not isinstance(value, (list, tuple)):
                continue
            label = "also running" if key == "parallel_with" else key
            for item in value[:6]:
                print(f"        {label}: {item}")
            if len(value) > 6:
                print(f"        {label}: ... and {len(value) - 6} more")

    return observer


def print_report(result: RunResult) -> None:
    state = result.state
    print()
    print("=" * 72)
    print(f"  {result.scenario.title}")
    print(f"  run {state.run_id}  |  provider {result.provider}  |  {state.duration_ms}ms")
    print("=" * 72)

    counts = state.counts()
    print("\n  tasks")
    for status, count in sorted(counts.items()):
        print(f"    {status:<20} {count}")

    print("\n  artifacts")
    by_kind: dict[str, int] = {}
    for artifact in state.artifacts.values():
        by_kind[artifact.kind.value] = by_kind.get(artifact.kind.value, 0) + 1
    for kind, count in sorted(by_kind.items()):
        print(f"    {kind:<20} {count}")
    total_lines = sum(a.lines for a in state.artifacts.values())
    print(f"    {'total lines':<20} {total_lines}")

    report = state.blackboard.get("final_validation_report")
    if report is not None:
        print(f"\n  validation: {report.status.value} after {report.attempt} round(s)")
        for check in report.checks:
            print(f"    {check.name:<20} {check.status.value:<6} {check.detail}")

    if state.approvals:
        print("\n  approvals")
        for decision in state.approvals:
            verdict = "approved" if decision.approved else "REJECTED"
            print(f"    {decision.task_id:<12} {verdict:<10} by {decision.approver}")

    print(f"\n  status:    {state.status.value}")
    print(f"  workspace: {result.workspace}")
    print(f"  run dir:   {result.run_dir}")
    print(f"  trace:     {result.run_dir / 'trace.jsonl'}  ({len(state.events)} events)")

    if state.status == RunStatus.NEEDS_CLARIFICATION:
        questions = [
            e for e in state.events if e.type == EventType.CLARIFICATION_REQUESTED
        ]
        print("\n  The run stopped because it will not guess. Answer and re-run:")
        for event in questions:
            for question in event.data.get("questions", []):
                print(f"    - {question}")
        ident = question.split(":")[0] if questions else "AMB-001"
        print(f"\n    asep {result.scenario.name} --answer '{ident}=your answer'")
    print()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # pragma: no cover - console encoding
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)

    if args.list:
        print(_scenario_help())
        return 0

    config = RunConfig(
        scenario=args.scenario,
        mode=args.mode,
        model=args.model,
        requirement=args.requirement,
        out_dir=Path(args.out),
        workspace=Path(args.workspace) if args.workspace else None,
        auto_approve=not args.approve,
        interactive_approval=args.approve,
        approve_threshold=RiskLevel(args.threshold),
        assume_defaults=not args.no_assume,
        answers=_parse_answers(args.answer),
        run_tests=not args.no_tests,
        test_timeout_s=args.test_timeout,
        max_repair_rounds=args.max_repair_rounds,
        max_workers=args.max_workers,
        inject_failures=set(args.inject_failure),
        resume=args.resume,
    )

    try:
        result = run(config, observer=make_observer(quiet=args.quiet, as_json=args.json))
    except (FileNotFoundError, RequirementNotScripted) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\ninterrupted", file=sys.stderr)
        return 130

    if args.json:
        print(json.dumps({"status": result.state.status.value, "run_dir": str(result.run_dir)}))
    else:
        print_report(result)

    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
