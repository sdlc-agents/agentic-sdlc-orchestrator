from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from ..models import (
    Artifact,
    CheckStatus,
    EventType,
    RiskLevel,
    RunState,
    RunStatus,
    Task,
    TaskStatus,
    ValidationReport,
)
from .errors import (
    AgentError,
    ClarificationRequired,
    ContractError,
    DependencyError,
    FatalError,
    GuardrailViolation,
)
from .graph import CycleError, TaskGraph
from .policy import Policy


@dataclass
class AgentContext:
    task: Task
    state: RunState
    workspace: Path
    provider: object
    attempt: int = 1
    previous_error: str | None = None
    findings: list = field(default_factory=list)

    def read(self, key: str):
        return self.state.blackboard.require(key)

    def get(self, key: str, default=None):
        return self.state.blackboard.get(key, default)


@dataclass
class AgentResult:
    writes: dict = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    note: str = ""
    follow_up_tasks: list[Task] = field(default_factory=list)


class Agent:
    name: str = "agent"

    def run(self, ctx: AgentContext) -> AgentResult:  # pragma: no cover - interface
        raise NotImplementedError


Observer = Callable[[object], None]


class Engine:
    """Dependency-aware scheduler with contract enforcement and repair.

    Execution loops over *ready* tasks rather than following a fixed sequence,
    so the graph can grow while it runs. Coordination is the blackboard, the
    declared read/write contracts, and validation findings that become nodes.
    """

    def __init__(
        self,
        graph: TaskGraph,
        agents: dict[str, Agent],
        state: RunState,
        workspace: Path,
        provider: object,
        policy: Policy | None = None,
        max_workers: int = 4,
        max_repair_rounds: int = 2,
        retry_backoff_s: float = 0.0,
        observer: Observer | None = None,
    ):
        self.graph = graph
        self.agents = agents
        self.state = state
        self.workspace = workspace
        self.provider = provider
        self.policy = policy or Policy(auto_approve=True)
        self.max_workers = max_workers
        self.max_repair_rounds = max_repair_rounds
        self.retry_backoff_s = retry_backoff_s
        self.observer = observer
        self.repair_rounds = 0
        self._last_validation: str | None = None

    # ------------------------------------------------------------------ run

    def run(self) -> RunState:
        self._emit(
            EventType.RUN_STARTED,
            f"run {self.state.run_id} ({self.state.mode} mode, {len(self.graph)} tasks)",
            tasks=len(self.graph),
            levels=len(self.graph.levels()),
            critical_path=self.graph.critical_path(),
        )
        self.state.tasks = {t.id: t for t in self.graph.tasks}

        try:
            while True:
                ready = sorted(self.graph.ready(), key=lambda t: t.id)
                if not ready:
                    if self._skip_unreachable():
                        continue
                    break

                approved = self._gate(ready)
                if not approved:
                    continue

                self._dispatch(approved)
                self._react_to_validation()
                self.state.tasks = {t.id: t for t in self.graph.tasks}

        except ClarificationRequired as exc:
            self.state.status = RunStatus.NEEDS_CLARIFICATION
            # The task did not fail — it is waiting on a person. Recording it as
            # still running would leave the persisted record claiming work is in
            # flight after the process has exited.
            for task in self.graph.tasks:
                if task.status == TaskStatus.RUNNING:
                    task.status = TaskStatus.PENDING
            self._emit(
                EventType.CLARIFICATION_REQUESTED,
                "run halted: blocking ambiguities require a human answer",
                questions=exc.questions,
            )
            return self._finish()

        return self._finish()

    # -------------------------------------------------------------- stages

    def _gate(self, ready: list[Task]) -> list[Task]:
        """Resolve approvals sequentially before any concurrent dispatch."""
        approved: list[Task] = []
        for task in ready:
            if not self.policy.requires_approval(task):
                approved.append(task)
                continue

            task.status = TaskStatus.AWAITING_APPROVAL
            self._emit(
                EventType.APPROVAL_REQUESTED,
                f"{task.id} needs approval (risk={self.policy.classify(task).value})",
                task_id=task.id,
                agent=task.agent,
            )
            decision = self.policy.request(task, self._approval_context(task))
            self.state.approvals.append(decision)

            if decision.approved:
                task.status = TaskStatus.PENDING
                approved.append(task)
                self._emit(
                    EventType.APPROVAL_GRANTED,
                    f"{task.id} approved by {decision.approver}",
                    task_id=task.id,
                )
            else:
                task.status = TaskStatus.REJECTED
                task.error = f"rejected by {decision.approver}: {decision.reason}"
                self._emit(
                    EventType.APPROVAL_DENIED,
                    f"{task.id} rejected by {decision.approver}",
                    task_id=task.id,
                )
        return approved

    def _dispatch(self, batch: list[Task]) -> None:
        for task in batch:
            task.status = TaskStatus.RUNNING
            self._emit(
                EventType.TASK_STARTED,
                f"{task.id} {task.title}",
                task_id=task.id,
                agent=task.agent,
                attempt=task.attempts + 1,
                parallel_with=[t.id for t in batch if t.id != task.id],
            )

        if len(batch) == 1:
            results = [self._execute(batch[0])]
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                results = list(pool.map(self._execute, batch))

        # strict: a result missing for a dispatched task would otherwise leave
        # that task silently stuck in RUNNING forever.
        for task, (result, error, elapsed) in zip(batch, results, strict=True):
            task.duration_ms = elapsed
            if error is None:
                self._commit(task, result)
            else:
                self._handle_failure(task, error)

    def _execute(self, task: Task):
        started = time.perf_counter()
        task.attempts += 1
        try:
            agent = self.agents.get(task.agent)
            if agent is None:
                raise FatalError(f"no agent registered for '{task.agent}'")

            missing = [k for k in task.reads if not self.state.blackboard.has(k)]
            if missing:
                raise DependencyError(
                    f"{task.id} declares reads {missing} that are not on the blackboard"
                )

            ctx = AgentContext(
                task=task,
                state=self.state,
                workspace=self.workspace,
                provider=self.provider,
                attempt=task.attempts,
                previous_error=task.error,
                findings=self.state.blackboard.get("open_findings", []),
            )
            result = agent.run(ctx)

            unwritten = [k for k in task.writes if k not in result.writes]
            if unwritten:
                raise ContractError(
                    f"{task.id} promised to write {unwritten} but did not"
                )

            for artifact in result.artifacts:
                violations = self.policy.screen(artifact.content)
                if violations:
                    raise GuardrailViolation(
                        f"{artifact.path} contains forbidden operation(s): {violations}"
                    )

            return result, None, int((time.perf_counter() - started) * 1000)

        except ClarificationRequired:
            raise
        except AgentError as exc:
            return None, exc, int((time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001 - agent bugs must not kill the run
            return None, FatalError(f"{type(exc).__name__}: {exc}"), int(
                (time.perf_counter() - started) * 1000
            )

    def _commit(self, task: Task, result: AgentResult) -> None:
        for key, value in result.writes.items():
            self.state.blackboard.put(key, value)
        for artifact in result.artifacts:
            self.state.add_artifact(artifact)

        task.status = TaskStatus.SUCCEEDED
        task.error = None
        if task.agent == "validation":
            self._last_validation = task.id
        self._emit(
            EventType.TASK_SUCCEEDED,
            f"{task.id} {result.note or task.title}",
            task_id=task.id,
            agent=task.agent,
            artifacts=[a.path for a in result.artifacts],
            wrote=sorted(result.writes),
            duration_ms=task.duration_ms,
        )

        for follow_up in result.follow_up_tasks:
            self._inject(follow_up, reason=f"requested by {task.id}")

    def _handle_failure(self, task: Task, error: Exception) -> None:
        task.error = f"{type(error).__name__}: {error}"
        retryable = isinstance(error, AgentError) and error.retryable

        if retryable and task.retries_left > 0:
            task.status = TaskStatus.PENDING
            self._emit(
                EventType.TASK_RETRIED,
                f"{task.id} failed ({type(error).__name__}), retry "
                f"{task.attempts}/{task.max_attempts} with error context attached",
                task_id=task.id,
                agent=task.agent,
                error=str(error),
            )
            if self.retry_backoff_s:
                time.sleep(self.retry_backoff_s * task.attempts)
            return

        task.status = TaskStatus.FAILED
        self._emit(
            EventType.TASK_FAILED,
            f"{task.id} failed permanently: {task.error}",
            task_id=task.id,
            agent=task.agent,
            error=task.error,
            retryable=retryable,
        )
        if isinstance(error, (FatalError, GuardrailViolation, DependencyError)):
            self._emit(
                EventType.ESCALATED,
                f"{task.id} escalated to human: {type(error).__name__} is not "
                "recoverable by the orchestrator",
                task_id=task.id,
            )

    # ------------------------------------------------------- repair feedback

    def _react_to_validation(self) -> None:
        """Turn validation findings into graph nodes.

        This is the cross-step coordination the pipeline shape cannot express:
        a late task rewrites the plan for an earlier stage and re-runs it.
        """
        report: ValidationReport | None = self.state.blackboard.get("validation_report")
        if report is None:
            return

        if report.status != CheckStatus.FAIL:
            self.state.blackboard.put("open_findings", [])
            self.state.blackboard.put("final_validation_report", report)
            self.state.blackboard.put("validation_report", None)
            self._emit(
                EventType.VALIDATION_PASSED,
                f"validation passed after {self.repair_rounds} repair round(s) "
                f"({len(report.checks)} checks, {len(report.warnings)} warning(s))",
                warnings=[f.message for f in report.warnings],
            )
            return

        if not report.repairable:
            self._emit(
                EventType.ESCALATED,
                f"validation failed with {len(report.errors)} error(s) and no "
                "machine-actionable repair hint; escalating to human review",
                findings=[f.message for f in report.errors],
            )
            self.state.blackboard.put("final_validation_report", report)
            self.state.blackboard.put("validation_report", None)
            return

        if self.repair_rounds >= self.max_repair_rounds:
            self._emit(
                EventType.ESCALATED,
                f"repair budget exhausted after {self.repair_rounds} round(s); "
                "remaining findings require human review",
                findings=[f.message for f in report.errors],
            )
            self.state.blackboard.put("final_validation_report", report)
            self.state.blackboard.put("validation_report", None)
            return

        self.repair_rounds += 1
        round_no = self.repair_rounds
        findings = report.repairable

        self._emit(
            EventType.VALIDATION_FAILED,
            f"validation round {round_no} found {len(report.errors)} error(s); "
            f"{len(findings)} are machine-repairable",
            findings=[f.message for f in report.errors],
        )

        repair = Task(
            id=f"T-900-R{round_no}",
            title=f"Repair {len(findings)} validation finding(s)",
            agent="repair",
            depends_on=[],
            reads=["artifacts_index", "api_contract"],
            writes=["repair_summary"],
            risk=RiskLevel.MEDIUM,
            origin="repair",
            rationale="scheduled by the engine from structured validation findings",
        )
        revalidate = Task(
            id=f"T-901-R{round_no}",
            title=f"Re-validate after repair round {round_no}",
            agent="validation",
            depends_on=[repair.id],
            reads=["artifacts_index"],
            writes=["validation_report"],
            origin="repair",
            rationale="closes the loop; a repair is not trusted until re-checked",
        )

        self.state.blackboard.put("open_findings", findings)
        self.state.blackboard.put("validation_report", None)
        self._inject(repair, reason=f"validation round {round_no}")
        self._inject(revalidate, reason=f"validation round {round_no}")
        rewired = self._rewire_onto(revalidate.id)
        self._emit(
            EventType.REPAIR_SCHEDULED,
            f"injected {repair.id} and {revalidate.id} into the running graph"
            + (f"; moved {', '.join(rewired)} behind {revalidate.id}" if rewired else ""),
            round=round_no,
            rewired=rewired,
        )

    def _rewire_onto(self, revalidate_id: str) -> list[str]:
        """Move anything still waiting on the old verdict behind the new one.

        A task depending on validation becomes ready the moment validation
        *finishes* — including when it finishes by failing. Without rewiring it
        would run beside the repair it triggered, reporting on a workspace being
        rewritten underneath it.
        """
        if self._last_validation is None:
            return []
        rewired: list[str] = []
        for dependent in self.graph.dependents(self._last_validation):
            if dependent.status != TaskStatus.PENDING or dependent.id == revalidate_id:
                continue
            try:
                self.graph.add_dependency(dependent.id, revalidate_id)
            except CycleError:
                continue
            rewired.append(dependent.id)
        return sorted(rewired)

    def _inject(self, task: Task, reason: str) -> None:
        try:
            self.graph.inject(task)
        except CycleError as exc:
            self._emit(
                EventType.ESCALATED,
                f"refused to inject {task.id}: {exc}",
                task_id=task.id,
            )
            return
        self.state.tasks[task.id] = task
        self._emit(
            EventType.GRAPH_MUTATED,
            f"+{task.id} ({task.agent}) — {reason}",
            task_id=task.id,
            agent=task.agent,
            depends_on=task.depends_on,
        )

    # ------------------------------------------------------------- helpers

    def _skip_unreachable(self) -> bool:
        blocked = self.graph.blocked_by_failure()
        if not blocked:
            return False
        for task in blocked:
            task.status = TaskStatus.SKIPPED
            task.error = "upstream task failed or was rejected"
            self._emit(
                EventType.TASK_SKIPPED,
                f"{task.id} skipped: {task.error}",
                task_id=task.id,
                agent=task.agent,
            )
        return True

    def _approval_context(self, task: Task) -> str:
        lines = [task.rationale] if task.rationale else []
        if task.covers:
            lines.append(f"covers: {', '.join(task.covers)}")
        if task.depends_on:
            lines.append(f"after: {', '.join(task.depends_on)}")
        arch = self.state.blackboard.get("architecture")
        if task.agent == "implementation" and arch is not None:
            lines.append(f"architecture: {arch.style}")
        return "\n".join(lines)

    def _finish(self) -> RunState:
        if self.state.status == RunStatus.RUNNING:
            failed = [
                t
                for t in self.graph.tasks
                if t.status in (TaskStatus.FAILED, TaskStatus.SKIPPED)
            ]
            rejected = [t for t in self.graph.tasks if t.status == TaskStatus.REJECTED]
            report: ValidationReport | None = self.state.blackboard.get(
                "final_validation_report"
            )
            if rejected:
                self.state.status = RunStatus.REJECTED_BY_HUMAN
            elif failed or (report is not None and report.status == CheckStatus.FAIL):
                self.state.status = RunStatus.FAILED
            else:
                self.state.status = RunStatus.SUCCEEDED

        self.state.finished_at = time.time()
        self.state.tasks = {t.id: t for t in self.graph.tasks}
        self._emit(
            EventType.RUN_FINISHED,
            f"run {self.state.status.value} in {self.state.duration_ms}ms",
            status=self.state.status.value,
            counts=self.state.counts(),
            artifacts=len(self.state.artifacts),
            repair_rounds=self.repair_rounds,
        )
        return self.state

    def _emit(self, type: EventType, message: str, **kwargs):
        task_id = kwargs.pop("task_id", None)
        agent = kwargs.pop("agent", None)
        event = self.state.emit(type, message, task_id=task_id, agent=agent, **kwargs)
        if self.observer:
            self.observer(event)
        return event


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]
