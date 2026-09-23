from __future__ import annotations

from ..models import Task, TaskStatus


class CycleError(ValueError):
    pass


class TaskGraph:
    """A mutable DAG of tasks.

    Mutability is the point: the validation/repair loop injects new tasks into a
    graph that is already executing, which is what separates this from a fixed
    pipeline. Every mutation is re-validated for cycles before it is accepted.
    """

    def __init__(self, tasks: list[Task] | None = None):
        self._tasks: dict[str, Task] = {}
        for task in tasks or []:
            self.add(task)
        if self._tasks:
            self.assert_acyclic()

    def add(self, task: Task) -> None:
        if task.id in self._tasks:
            raise ValueError(f"duplicate task id: {task.id}")
        unknown = [d for d in task.depends_on if d not in self._tasks]
        if unknown and self._tasks:
            raise ValueError(f"{task.id} depends on unknown task(s): {unknown}")
        self._tasks[task.id] = task

    def inject(self, task: Task) -> None:
        """Add a task to a running graph, rejecting the change if it cycles."""
        snapshot = dict(self._tasks)
        self.add(task)
        try:
            self.assert_acyclic()
        except CycleError:
            self._tasks = snapshot
            raise

    def add_dependency(self, task_id: str, depends_on: str) -> None:
        task = self[task_id]
        if depends_on in task.depends_on:
            return
        task.depends_on.append(depends_on)
        try:
            self.assert_acyclic()
        except CycleError:
            task.depends_on.remove(depends_on)
            raise

    def __getitem__(self, task_id: str) -> Task:
        return self._tasks[task_id]

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._tasks

    def __len__(self) -> int:
        return len(self._tasks)

    @property
    def tasks(self) -> list[Task]:
        return list(self._tasks.values())

    def dependents(self, task_id: str) -> list[Task]:
        return [t for t in self._tasks.values() if task_id in t.depends_on]

    def assert_acyclic(self) -> None:
        colour: dict[str, int] = {}

        def visit(node: str, trail: list[str]) -> None:
            state = colour.get(node, 0)
            if state == 1:
                cycle = trail[trail.index(node) :] + [node]
                raise CycleError(f"dependency cycle: {' -> '.join(cycle)}")
            if state == 2:
                return
            colour[node] = 1
            for dep in self._tasks[node].depends_on:
                if dep in self._tasks:
                    visit(dep, trail + [node])
            colour[node] = 2

        for task_id in self._tasks:
            visit(task_id, [])

    def ready(self) -> list[Task]:
        """Pending tasks whose dependencies have all succeeded."""
        out = []
        for task in self._tasks.values():
            if task.status != TaskStatus.PENDING:
                continue
            if all(
                self._tasks[d].status == TaskStatus.SUCCEEDED
                for d in task.depends_on
                if d in self._tasks
            ):
                out.append(task)
        return out

    def blocked_by_failure(self) -> list[Task]:
        """Pending tasks that can never run because an ancestor died."""
        dead = {
            t.id
            for t in self._tasks.values()
            if t.status in (TaskStatus.FAILED, TaskStatus.REJECTED)
        }
        if not dead:
            return []
        out, changed = [], True
        blocked: set[str] = set()
        while changed:
            changed = False
            for task in self._tasks.values():
                if task.status != TaskStatus.PENDING or task.id in blocked:
                    continue
                if any(d in dead or d in blocked for d in task.depends_on):
                    blocked.add(task.id)
                    out.append(task)
                    changed = True
        return out

    def levels(self) -> list[list[str]]:
        """Topological levels; tasks within a level are independent."""
        self.assert_acyclic()
        remaining = {t.id: set(t.depends_on) & set(self._tasks) for t in self._tasks.values()}
        out: list[list[str]] = []
        while remaining:
            layer = sorted(tid for tid, deps in remaining.items() if not deps)
            if not layer:
                raise CycleError("unresolvable dependencies")
            out.append(layer)
            for tid in layer:
                remaining.pop(tid)
            for deps in remaining.values():
                deps.difference_update(layer)
        return out

    def critical_path(self) -> list[str]:
        """Longest dependency chain — the floor on wall-clock time."""
        memo: dict[str, list[str]] = {}

        def longest(task_id: str) -> list[str]:
            if task_id in memo:
                return memo[task_id]
            deps = [d for d in self._tasks[task_id].depends_on if d in self._tasks]
            best: list[str] = []
            for dep in deps:
                candidate = longest(dep)
                if len(candidate) > len(best):
                    best = candidate
            memo[task_id] = best + [task_id]
            return memo[task_id]

        return max((longest(t) for t in self._tasks), key=len, default=[])

    def to_mermaid(self) -> str:
        lines = ["graph TD"]
        for task in self._tasks.values():
            label = task.title.replace('"', "'")
            lines.append(f'    {task.id}["{task.id}<br/>{label}"]')
        for task in self._tasks.values():
            for dep in task.depends_on:
                lines.append(f"    {dep} --> {task.id}")
        return "\n".join(lines)
