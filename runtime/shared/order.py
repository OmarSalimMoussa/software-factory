"""The two orders tasks follow: the queue's, and the order runs take places in.

The queue's order, for the queue and its proposals and for merge turns: severe first (oldest first), then boosted
(latest boost first), then the owner's order among approved runs, then the goal item's rank in GOAL.md, then the
score (higher first), then age (older first).

The seat order, for starts, revivals and benches: severe first (oldest first), then the goal item's rank, then the
owner's order among approved runs, then the score, then age. A bench reads only its first part, severity and the goal
item, which nothing changes when a run starts: so a run never benches the run that benched it.
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


def _parts(task: Task, ranking: Ranking) -> tuple[int, str, int, int, float]:
    """Both orders' shared parts, each turned so that what comes first sorts first."""
    severe = 0 if task.severity == "severe" else 1
    owner = ranking.approved.index(task.id) if task.id in ranking.approved else _FAR
    score = -task.score if task.score is not None else float("inf")
    return (severe, task.created if severe == 0 else "", goal_rank(task.goal_item, ranking.goals), owner, score)


def rank(task: Task, ranking: Ranking) -> tuple[int, str, str, int, int, float, str]:
    severe, age, goal, owner, score = _parts(task, ranking)
    boost = _later_first(task.boosted_at) if task.boosted_at else "\U0010ffff"
    return (severe, age, boost, owner, goal, score, task.created)


def ordered(tasks: Sequence[Task], ranking: Ranking) -> list[Task]:
    return sorted(tasks, key=lambda t: rank(t, ranking))


def seat_rank(task: Task, ranking: Ranking) -> tuple[int, str, int, int, float, str]:
    return (*_parts(task, ranking), task.created)


def may_bench(new: Task, seated: Task, ranking: Ranking) -> bool:
    """Whether ``new`` may push ``seated`` out of its place: only for severe work, or work for a higher goal item."""
    return seat_rank(new, ranking)[:3] < seat_rank(seated, ranking)[:3]
