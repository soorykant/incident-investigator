"""Plain-Python task-DAG rules (Q9) and the deterministic tool-call order.

`ready_tasks` picks the pending tasks whose dependencies are all finished. A failed
dependency counts as finished: the DAG continues without that agent's result (Q16).

`ToolTurns` makes the failure schedule ("the Nth call to a tool fails") reproducible
when agents run in parallel. Tasks started together form a wave and get a rank by
plan order. The k-th tool call of the task with rank r has the key (k, r), and calls
are dispatched in key order: round-robin over the wave, plan order within a round.
A call waits only until every call with a smaller key has been made, or the task that
would make it has finished. LLM calls never wait, so tasks still overlap; only the
(fast) tool dispatch is ordered. The order then depends only on what each agent
asks, not on thread timing.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from incident_investigator.blackboard import Task

FINISHED = ("done", "failed")


def ready_tasks(tasks: list[Task]) -> list[Task]:
    status = {t.id: t.status for t in tasks}
    return [
        t
        for t in tasks
        if t.status == "pending" and all(status.get(d) in FINISHED for d in t.depends_on)
    ]


def blocked_tasks(tasks: list[Task]) -> list[Task]:
    """Pending tasks that can never start (a dependency is missing)."""
    ids = {t.id for t in tasks}
    return [t for t in tasks if t.status == "pending" and any(d not in ids for d in t.depends_on)]


class ToolTurns:
    """Orders the tool calls of one wave of parallel tasks. Thread-safe."""

    def __init__(self, task_ids: list[str]) -> None:
        self._rank = {task_id: rank for rank, task_id in enumerate(task_ids)}
        self._made = dict.fromkeys(task_ids, 0)
        self._finished: set[str] = set()
        self._cond = threading.Condition()

    def _my_turn(self, task_id: str) -> bool:
        k, rank = self._made[task_id] + 1, self._rank[task_id]
        for other, other_rank in self._rank.items():
            if other == task_id or other in self._finished:
                continue
            needed = k if other_rank < rank else k - 1
            if self._made[other] < needed:
                return False
        return True

    def call[R](self, task_id: str, dispatch: Callable[[], R]) -> R:
        with self._cond:
            self._cond.wait_for(lambda: self._my_turn(task_id))
        try:
            return dispatch()
        finally:
            with self._cond:
                self._made[task_id] += 1
                self._cond.notify_all()

    def finish(self, task_id: str) -> None:
        with self._cond:
            self._finished.add(task_id)
            self._cond.notify_all()
