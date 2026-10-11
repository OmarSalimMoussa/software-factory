"""A task's priority score (intake spec, "The score"): five parts from 0 to 1, weighed by ``intake.weights`` into a
score out of 100. Severity and goal rank decide the order; the other three break ties.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from runtime.queue.formats import epoch
from runtime.shared.models import Request, Task
from runtime.shared.settings import Settings

DAY_S = 86400


def _rising(count: int, full: int) -> float:
    """0 at one, rising evenly to 1 at ``full`` or more."""
    return min(1.0, max(0, count - 1) / (full - 1))


def parts(
    task: Task, requests: Sequence[Request], goals: Mapping[str, int], now: float, settings: Settings
) -> dict[str, float]:
    """``goals`` is each open goal item's rank by its name: a done or unknown item adds nothing."""
    if {r.id for r in requests} != set(task.requests):
        raise ValueError(f"{task.id}: score needs exactly the task's own requests")
    severity = settings.get_float("intake.minor_severity") if task.severity == "minor" else 1.0
    rank, count = goals.get(task.goal_item), len(goals)
    goal = (count - rank + 1) / count if rank is not None else 0.0
    users = {r.from_role for r in requests if r.inbox == "users"}
    oldest = min(epoch(r.at) for r in requests)
    age_days = max(0.0, (now - oldest) / DAY_S)
    return {
        "severity": severity,
        "goal": goal,
        "requests": _rising(len(requests), settings.get_int("intake.requests_full")),
        "users": _rising(len(users), settings.get_int("intake.users_full")),
        "age": min(1.0, age_days / settings.get_int("intake.age_full_days")),
    }


def score(
    task: Task, requests: Sequence[Request], goals: Mapping[str, int], now: float, settings: Settings
) -> float | None:
    """The score out of 100, to one decimal; unknown when every weight is zero."""
    weights: dict[str, float] = settings["intake.weights"]
    total = sum(weights.values())
    if total <= 0:
        return None
    found = parts(task, requests, goals, now, settings)
    return round(100 * sum(weights[k] * found[k] for k in found) / total, 1)
