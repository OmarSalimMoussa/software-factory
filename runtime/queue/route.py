"""The dispatch ladder (routing spec): the lowest rung whose every condition holds, read from a task's facts.

The orchestrator may plan another rung with a reason; ``factory propose`` checks that against this ladder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from runtime.queue.formats import SIGN_OFF_CLASSES
from runtime.shared.models import Task
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings

CAUSE_AT_LINE = re.compile(r"[\w./-]+\.\w+:\d+")
WORDS = {0: "No build", 1: "Small, no spec", 2: "Large, no spec", 3: "Spec, auto sign-off", 4: "Spec, owner sign-off"}
# The role whose model a rung's first session runs; after a spec the build is routed again by its slice count.
ROLES = {1: "implementer_small", 2: "implementer_large", 3: "designer", 4: "designer"}


@dataclass(frozen=True, slots=True)
class Rung:
    n: int
    word: str
    holds: bool
    unmet: tuple[str, ...]


def ladder(task: Task, settings: Settings) -> list[Rung]:
    """Every rung with whether it holds for this task and, when it does not, which conditions fail."""
    if task.slices is None or not task.surfaces:
        raise Refused("routing-facts", f"{task.id} has no slice estimate or surfaces: add them with task facts")
    small, large = settings.get_int("routing.small_max_slices"), settings.get_int("routing.large_max_slices")
    plain = task.owner_class == "none"
    conditions: dict[int, list[tuple[bool, str]]] = {
        0: [(task.slices == 0, "code needs to change")],
        1: [
            (bool(CAUSE_AT_LINE.search(task.cause)), "the cause is not known to the file and line"),
            (task.slices <= small, f"more than {small} slices"),
            ("access" not in task.surfaces, "it touches access"),
            (plain, f"it is a {task.owner_class} change"),
        ],
        2: [
            (bool(task.cause), "the cause is not known"),
            (task.slices <= large, f"more than {large} slices"),
            (plain, f"it is a {task.owner_class} change"),
        ],
        3: [(task.owner_class not in SIGN_OFF_CLASSES, "it needs the owner's sign-off on its spec")],
        4: [],
    }
    return [
        Rung(n, WORDS[n], all(ok for ok, _ in conds), tuple(why for ok, why in conds if not ok))
        for n, conds in conditions.items()
    ]


def route(task: Task, settings: Settings) -> int:
    return next(r.n for r in ladder(task, settings) if r.holds)


def model_for(settings: Settings, rung: int) -> str:
    """The model a run at ``rung`` starts with; rung 0 builds nothing."""
    if rung not in ROLES:
        raise Refused("rung", f"rung {rung} starts no session")
    return settings.model(ROLES[rung])[0]
