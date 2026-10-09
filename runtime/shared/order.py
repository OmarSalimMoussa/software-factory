"""The one order every list of tasks follows: the queue, proposals, approved starts and bench.

Severe first (oldest first), then boosted (latest boost first), then the owner's order among approved runs, then the
goal item's rank in GOAL.md, then the score (higher first), then age (older first).
"""

from __future__ import annotations

from collections.abc import Sequence

from runtime.shared.models import Task

_FAR = 10**9


def goal_rank(goal_item: str) -> int:
    """G1 is 1; an empty or unknown item sorts after every ranked one."""
    if goal_item.startswith("G") and goal_item[1:].isdigit():
        return int(goal_item[1:])
    return _FAR


def _later_first(iso: str) -> str:
    """An ISO time turned so a plain ascending sort puts the later time first."""
    return "".join(chr(0x10FFFF - ord(c)) for c in iso)


def rank(task: Task, approved_order: Sequence[str]) -> tuple[int, str, str, int, int, float, str]:
    severe = 0 if task.severity == "severe" else 1
    severe_age = task.created if severe == 0 else ""
    boost = _later_first(task.boosted_at) if task.boosted_at else "\U0010ffff"
    owner = approved_order.index(task.id) if task.id in approved_order else _FAR
    score = -task.score if task.score is not None else float("inf")
    return (severe, severe_age, boost, owner, goal_rank(task.goal_item), score, task.created)


def ordered(tasks: Sequence[Task], approved_order: Sequence[str]) -> list[Task]:
    return sorted(tasks, key=lambda t: rank(t, approved_order))


def outranks(a: Task, b: Task, approved_order: Sequence[str]) -> bool:
    """Whether ``a`` comes before ``b`` in the one order (bench uses it to choose who makes room)."""
    return rank(a, approved_order) < rank(b, approved_order)
