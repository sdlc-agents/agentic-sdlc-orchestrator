"""Assemble a run and execute it.

Kept separate from the CLI so a run is callable as a function: the tests drive
this module directly rather than shelling out, which is also what makes the
platform usable as a library.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .agents import build_registry
from .agents import summary as summary_agent
from .metrics import collect as collect_metrics
from .metrics import render as render_metrics
from .models import ArtifactKind, Event, EventType, RiskLevel, RunState, RunStatus, TaskStatus
from .orchestration import (
    CycleError,
    Engine,
    Policy,
    RunRecorder,
    TaskGraph,
    cli_approval_handler,
    new_run_id,
)
from .providers import build_provider
from .providers.base import Provider
from .scenarios import Scenario
from .scenarios import get as get_scenario
from .tools.workspace import Workspace

Observer = Callable[[Event], None]


@dataclass
class RunConfig:
    scenario: str = "url_shortener"
    mode: str = "mock"
    model: str | None = None
    requirement: str | None = None
    out_dir: Path = Path("runs")
    workspace: Path | None = None
    auto_approve: bool = True
    interactive_approval: bool = False
    approve_threshold: RiskLevel = RiskLevel.MEDIUM
    assume_defaults: bool = True
    answers: dict[str, str] = field(default_factory=dict)
    run_tests: bool = True
    test_timeout_s: int = 180
    max_repair_rounds: int = 2
    max_workers: int = 4
    run_id: str | None = None
    # Agents whose first attempt should fail, to exercise recovery on demand.
    inject_failures: set[str] = field(default_factory=set)
    # Continue a previous run instead of starting a new one.
    resume: str | None = None


@dataclass
class RunResult:
    state: RunState
    run_dir: Path
    workspace: Path
    scenario: Scenario
    provider: str

    @property
    def ok(self) -> bool:
        return self.state.status.value == "succeeded"


def prepare_workspace(root: Path, scenario: Scenario) -> Workspace:
    """Start every run from a known state.

    A brownfield run is seeded with a copy of the target repository rather than
    pointed at the original. The agents then operate on the copy, so the worst
    outcome of a confused run is a bad workspace instead of a damaged codebase.
    """
    workspace = Workspace(root)
    workspace.reset()
    if scenario.seed_from is not None:
        source = Path(scenario.seed_from)
        if not source.exists():
            raise FileNotFoundError(
                f"scenario '{scenario.name}' seeds from {source}, which does not exist"
            )
        shutil.copytree(
            source,
            workspace.root,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "*.db"),
        )
    return workspace


def digest_tree(workspace: Workspace) -> dict[str, str]:
    """sha256 of every file currently in the workspace, keyed by relative path."""
    digests: dict[str, str] = {}
    for path in workspace.files("**/*"):
        rel = path.relative_to(workspace.root).as_posix()
        digests[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def build_engine(
    config: RunConfig,
    scenario: Scenario,
    state: RunState,
    workspace: Workspace,
    provider: Provider,
    observer: Observer | None = None,
) -> Engine:
    agents = build_registry(
        assume_defaults=config.assume_defaults,
        answers=config.answers,
        run_tests=config.run_tests,
        test_timeout_s=config.test_timeout_s,
        extra_checks=scenario.checks,
    )
    policy = Policy(
        approve_threshold=config.approve_threshold,
        auto_approve=config.auto_approve,
        handler=cli_approval_handler() if config.interactive_approval else None,
    )
    return Engine(
        graph=TaskGraph(scenario.graph_tasks()),
        agents=agents,
        state=state,
        workspace=workspace.root,
        provider=provider,
        policy=policy,
        max_workers=config.max_workers,
        max_repair_rounds=config.max_repair_rounds,
        observer=observer,  # type: ignore[arg-type]
    )


class RequirementNotScripted(ValueError):
    """The deterministic provider was handed a requirement it cannot answer."""


def _check_requirement_is_answerable(config: RunConfig, scenario: Scenario) -> None:
    """Refuse to replay a script against a requirement it was not written for.

    Given another requirement the mock would quote it back and return the
    scripted design anyway — a confident, unrelated answer. Refusing and naming
    `--mode openai` is the honest response.
    """
    if config.mode != "mock" or not config.requirement:
        return
    if config.requirement.strip() == scenario.requirement.strip():
        return
    raise RequirementNotScripted(
        f"--mode mock replays a fixed script for scenario '{scenario.name}' and "
        f"cannot answer a different requirement.\n\n"
        f"  scripted: {scenario.requirement}\n"
        f"  supplied: {config.requirement}\n\n"
        "Run with --mode openai to put a real model behind the same agents, or "
        "drop --requirement to use the scripted one."
    )


def run(config: RunConfig, observer: Observer | None = None) -> RunResult:
    scenario = get_scenario(config.scenario)
    _check_requirement_is_answerable(config, scenario)
    provider = build_provider(config.mode, config.model, config.inject_failures)

    if config.resume:
        state, recorder, workspace = _reopen(config, scenario)
    else:
        state, recorder, workspace = _start(config, scenario)

    engine = build_engine(config, scenario, state, workspace, provider, observer)
    if config.resume:
        _restore_progress(engine, state)

    def flushing_observer(event: Event) -> None:
        # Flush as events arrive rather than at the end: a run that is killed
        # halfway still leaves a readable trace of how far it got.
        if observer:
            observer(event)
        recorder.flush_events(state)

    engine.observer = flushing_observer  # type: ignore[assignment]

    if config.resume:
        done = sorted(
            t.id for t in state.tasks.values() if t.status is TaskStatus.SUCCEEDED
        )
        flushing_observer(
            state.emit(
                EventType.TASK_SKIPPED,
                f"resuming run {config.resume}; {len(done)} task(s) already done "
                "and will not be repeated",
                resumed=True,
                already_done=done,
            )
        )

    try:
        engine.run()
    finally:
        _persist(recorder, engine, state)

    return RunResult(
        state=state,
        run_dir=recorder.dir,
        workspace=workspace.root,
        scenario=scenario,
        provider=provider.describe(),
    )


def _start(config: RunConfig, scenario: Scenario):
    run_id = config.run_id or new_run_id()
    recorder = RunRecorder(Path(config.out_dir), run_id)
    workspace = prepare_workspace(
        Path(config.workspace) if config.workspace else recorder.dir / "workspace",
        scenario,
    )
    state = RunState(
        run_id=run_id,
        requirement=config.requirement or scenario.requirement,
        scenario=scenario.name,
        mode=config.mode,
    )
    # Fingerprint the workspace before any agent touches it. A refactor is
    # judged against this, and it has to be taken here rather than by an agent:
    # a baseline recorded by the thing being measured is not a baseline.
    state.blackboard.put("baseline_digests", digest_tree(workspace))
    return state, recorder, workspace


def _reopen(config: RunConfig, scenario: Scenario):
    """Pick up a previous run where it stopped.

    The workspace is left exactly as the earlier run left it, and tasks that
    already succeeded keep that status, so answering a clarification costs only
    the work that was actually blocked.
    """
    root = Path(config.out_dir)
    state = RunRecorder.load(root, config.resume)
    if state.scenario != scenario.name:
        raise RequirementNotScripted(
            f"run {config.resume} is scenario '{state.scenario}', not "
            f"'{scenario.name}'"
        )

    recorder = RunRecorder(root, config.resume)
    recorder.adopt(state)
    state.status = RunStatus.RUNNING
    state.finished_at = None

    workspace = Workspace(
        Path(config.workspace) if config.workspace else recorder.dir / "workspace"
    )
    # Duration should measure this session, not wall-clock since the run first
    # started, which may have been days ago.
    state.started_at = time.time()
    return state, recorder, workspace


def _restore_progress(engine: Engine, state: RunState) -> None:
    """Carry recorded task statuses onto the freshly built graph."""
    for task in engine.graph.tasks:
        recorded = state.tasks.get(task.id)
        if recorded is not None:
            task.status = recorded.status
            task.attempts = recorded.attempts
            task.duration_ms = recorded.duration_ms
            task.artifact_ids = list(recorded.artifact_ids)

    # Tasks the planner injected last time are not in the scenario graph; add
    # them back so their results are not recomputed.
    for task in state.tasks.values():
        if task.id not in engine.graph and task.status is TaskStatus.SUCCEEDED:
            try:
                engine.graph.inject(task)
            except (ValueError, CycleError):
                continue


def _refresh_summary(state: RunState) -> None:
    """Re-render the run report now that the run has a final status.

    The summary task runs inside the run it describes, so its own version said
    "running". Rebuilding from the same pure function fixes that.
    """
    existing = state.artifacts.get(summary_agent.SUMMARY_PATH)
    if existing is None:
        return
    try:
        _, refreshed = summary_agent.build(state, produced_by=existing.produced_by)
    except KeyError:
        return
    state.artifacts[refreshed.id] = refreshed


def _persist(recorder: RunRecorder, engine: Engine, state: RunState) -> None:
    _refresh_summary(state)
    recorder.save(state)
    recorder.write_text("graph.mmd", engine.graph.to_mermaid())

    # Derived from the trace the run already produced, so the numbers cannot
    # drift from what actually happened.
    metrics = collect_metrics(state)
    recorder.write_text("metrics.json", json.dumps(metrics.as_dict(), indent=2))
    recorder.write_text("metrics.md", render_metrics(metrics))
    # Reports describe the run, not the deliverable, so they live beside the
    # trace instead of in the workspace the next validation round would re-scan.
    for artifact in state.artifacts.values():
        if artifact.kind == ArtifactKind.REPORT:
            recorder.write_text(artifact.path, artifact.content)


__all__ = [
    "RequirementNotScripted",
    "RunConfig",
    "RunResult",
    "build_engine",
    "digest_tree",
    "prepare_workspace",
    "run",
]
