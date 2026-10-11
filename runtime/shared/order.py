"""The one order every list of tasks follows: the queue, proposals, approved starts and bench.

Severe first (oldest first), then boosted (latest boost first), then the owner's order among approved runs, then the
goal item's rank in GOAL.md, then the score (higher first), then age (older first).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from runtime.shared.models import Task

_FAR = 10**9


@dataclass(frozen=True, slots=True)
class Ranking:
    """What the order reads besides the tasks: the owner's order among approved runs, and each open goal item's
    rank in GOAL.md by its name."""

    approved: Sequence[str]
    goals: Mapping[str, int]


def goal_rank(goal_item: str, goals: Mapping[str, int]) -> int:
    """The item's rank; an empty, done or unknown item sorts after every ranked one."""
    return goals.get(goal_item, _FAR)


def _later_first(iso: str) -> str:
    """An ISO time turned so a plain ascending sort puts the later time first."""
    return "".join(chr(0x10FFFF - ord(c)) for c in iso)


def rank(task: Task, ranking: Ranking) -> tuple[int, str, str, int, int, float, str]:
    severe = 0 if task.severity == "severe" else 1
    severe_age = task.created if severe == 0 else ""
    boost = _later_first(task.boosted_at) if task.boosted_at else "\U0010ffff"
    owner = ranking.approved.index(task.id) if task.id in ranking.approved else _FAR
    score = -task.score if task.score is not None else float("inf")
    return (severe, severe_age, boost, owner, goal_rank(task.goal_item, ranking.goals), score, task.created)


def ordered(tasks: Sequence[Task], ranking: Ranking) -> list[Task]:
    return sorted(tasks, key=lambda t: rank(t, ranking))


def outranks(a: Task, b: Task, ranking: Ranking) -> bool:
    """Whether ``a`` comes before ``b`` in the one order (bench uses it to choose who makes room)."""
    return rank(a, ranking) < rank(b, ranking)
