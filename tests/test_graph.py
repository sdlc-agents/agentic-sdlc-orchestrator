"""The scheduler's view of the work.

These tests care about one property above the rest: the graph stays correct
while it is being mutated, because the repair loop adds nodes to a graph that is
already executing.
"""

from __future__ import annotations

import pytest
from conftest import graph, task

from asep.models import TaskStatus
from asep.orchestration import CycleError, TaskGraph


def test_ready_returns_only_tasks_whose_dependencies_succeeded():
    g = graph(task("T-001"), task("T-002", depends_on=["T-001"]))
    assert [t.id for t in g.ready()] == ["T-001"]

    g["T-001"].status = TaskStatus.SUCCEEDED
    assert [t.id for t in g.ready()] == ["T-002"]


def test_independent_tasks_are_ready_together():
    g = graph(
        task("T-001"),
        task("T-002", depends_on=["T-001"]),
        task("T-003", depends_on=["T-001"]),
    )
    g["T-001"].status = TaskStatus.SUCCEEDED
    assert sorted(t.id for t in g.ready()) == ["T-002", "T-003"]


def test_cycles_are_rejected_when_constructed():
    a = task("T-001", depends_on=["T-002"])
    b = task("T-002", depends_on=["T-001"])
    with pytest.raises((CycleError, ValueError)):
        TaskGraph([a, b])


def test_inject_rolls_back_a_task_that_would_create_a_cycle():
    g = graph(task("T-001"), task("T-002", depends_on=["T-001"]))
    g["T-001"].depends_on.append("T-900")

    with pytest.raises(CycleError):
        g.inject(task("T-900", depends_on=["T-002"]))

    # The rejected task must not be left behind in a half-applied state.
    assert "T-900" not in g
    assert len(g) == 2


def test_add_dependency_is_reverted_if_it_would_cycle():
    g = graph(task("T-001"), task("T-002", depends_on=["T-001"]))
    with pytest.raises(CycleError):
        g.add_dependency("T-001", "T-002")
    assert g["T-001"].depends_on == []


def test_blocked_by_failure_is_transitive():
    g = graph(
        task("T-001"),
        task("T-002", depends_on=["T-001"]),
        task("T-003", depends_on=["T-002"]),
        task("T-004"),
    )
    g["T-001"].status = TaskStatus.FAILED
    blocked = {t.id for t in g.blocked_by_failure()}
    assert blocked == {"T-002", "T-003"}


def test_levels_group_independent_work():
    g = graph(
        task("T-001"),
        task("T-002", depends_on=["T-001"]),
        task("T-003", depends_on=["T-001"]),
        task("T-004", depends_on=["T-002", "T-003"]),
    )
    assert g.levels() == [["T-001"], ["T-002", "T-003"], ["T-004"]]


def test_critical_path_is_the_longest_chain():
    g = graph(
        task("T-001"),
        task("T-002", depends_on=["T-001"]),
        task("T-003", depends_on=["T-002"]),
        task("T-004", depends_on=["T-001"]),
    )
    assert g.critical_path() == ["T-001", "T-002", "T-003"]


def test_duplicate_task_ids_are_rejected():
    g = graph(task("T-001"))
    with pytest.raises(ValueError):
        g.add(task("T-001"))
