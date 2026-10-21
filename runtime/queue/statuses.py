"""The one task status table (plan section 4.3, R1): every move a task may make, the event that drives it and who
may ask for it. A move not in the table is refused; "only forward" means only along these rows.

The warden writes every status. ``actor`` is who the warden acts for: the owner, the orchestrator, an implementer or
designer through its own verb, or the warden itself.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from runtime.shared.models import TASK_STATUSES, Task
from runtime.shared.protocol import Refused

NEW = ""  # a task not written yet
OWNER, ORCH, IMPL, DESIGN, WARDEN = "owner", "orchestrator", "implementer", "designer", "warden"
ACTORS = (OWNER, ORCH, IMPL, DESIGN, WARDEN)
RUNNING = frozenset({"designing", "building", "qa", "qa-clean", "turn"})
OPEN = RUNNING | {"approved", "review", "deprioritized", "decide"}
# A benched builder's task waits on its check, the owner or another task, not a place.
WAITS = frozenset({"merged", "blocked", "on-hold"})
NOT_CLOSED = frozenset(TASK_STATUSES) - {"closed"}


@dataclass(frozen=True, slots=True)
class Move:
    sources: frozenset[str]
    targets: frozenset[str]
    event: str
    actors: frozenset[str]


def _m(sources: set[str] | frozenset[str], targets: set[str], event: str, *actors: str) -> Move:
    return Move(frozenset(sources), frozenset(targets), event, frozenset(actors))


MOVES: tuple[Move, ...] = (
    _m({NEW}, {"queued"}, "triage", ORCH),
    _m({NEW}, {"queued"}, "fast-lane", ORCH),
    _m({"queued"}, {"proposed"}, "propose", ORCH),
    _m({"proposed"}, {"queued"}, "not-now", OWNER),
    _m({"proposed"}, {"queued"}, "void", WARDEN),
    _m({"proposed"}, {"approved"}, "approve", OWNER),
    _m({"approved"}, {"designing", "building"}, "start", WARDEN),
    # The outage bypass (routing spec): the one run that starts without an approval, on the warden's own probes.
    _m({"queued", "proposed", "approved"}, {"building"}, "outage", WARDEN),
    _m({"designing"}, {"review"}, "needs-owner", DESIGN, ORCH),
    _m({"review"}, {"building"}, "go", OWNER),
    _m({"designing", "review"}, {"building"}, "spec-approved", ORCH),
    _m({"building", "designing"}, {"deprioritized"}, "bench", WARDEN),
    _m({"deprioritized"}, {"building", "designing"}, "revive", WARDEN),
    _m({"building"}, {"qa"}, "qa-ready", IMPL),
    _m({"qa"}, {"qa-clean"}, "qa-clean", IMPL),
    _m({"qa-clean"}, {"turn"}, "turn", ORCH),
    _m({"turn"}, {"qa-clean"}, "turn-back", WARDEN),
    # The warden's alone: it finds the merge on the default branch, and a saved check of it that passed.
    _m({"turn"}, {"merged"}, "merged", WARDEN),
    _m({"merged"}, {"checked"}, "checked", WARDEN),
    _m({"checked"}, {"on-staging"}, "on-staging", WARDEN),
    _m({"on-staging"}, {"promoted"}, "promoted", WARDEN),
    # Prod pushed back, or staging taken to a build without the merge: the status follows what runs.
    _m({"promoted"}, {"on-staging"}, "left-prod", WARDEN),
    _m({"on-staging"}, {"checked"}, "left-staging", WARDEN),
    _m(RUNNING, {"decide"}, "limits", WARDEN),
    _m({"decide"}, set(RUNNING), "extend", OWNER),  # back to the status the spent limit stopped it in
    _m({"decide"}, {"parked"}, "park", OWNER),
    # What only the owner can give: off the default branch until it clears (queue/turn.py).
    _m({"building", "qa", "qa-clean", "turn", "merged", "blocked"}, {"blocked"}, "blocked", ORCH),
    _m({"blocked"}, {"building", "approved"}, "unblocked", OWNER, ORCH),
    # What another task makes (a seed, a scenario): off the default branch until that task's check passes.
    _m({"building", "qa", "qa-clean", "turn", "merged"}, {"on-hold"}, "on-hold", ORCH),
    _m({"on-hold"}, {"building", "approved"}, "released", WARDEN, ORCH),
    _m({"decide"}, {"proposed"}, "to-designer", OWNER),
    _m(OPEN | {"proposed"}, {"proposed"}, "rung-up", ORCH),
    _m(OPEN, {"proposed"}, "void", WARDEN),
    # A run that ended without finishing comes back as a proposal; the event names why for the task's log. A run
    # that never was one (prod's outage fix) has no why or picture to propose with, so it goes back to the queue.
    _m(OPEN, {"proposed", "queued"}, "stop", OWNER, ORCH),
    _m(OPEN, {"proposed", "queued"}, "outage-stopped", WARDEN),
    _m(OPEN, {"proposed", "queued"}, "crashed", WARDEN),
    _m(OPEN, {"proposed", "queued"}, "stopped", WARDEN),
    _m(NOT_CLOSED, {"closed"}, "drop", OWNER),
    _m(NOT_CLOSED, {"closed"}, "rung-0", ORCH),
    _m(NOT_CLOSED, {"closed"}, "noise", ORCH),
    _m(NOT_CLOSED, {"closed"}, "done", ORCH, WARDEN),
    # A reverted task goes back to its implementer, or, once that has ended, to a new one (runtime/queue/turn.py).
    _m({"merged", "checked", "on-staging"}, {"building", "approved"}, "reverted", ORCH, WARDEN),
    _m({"queued"}, {"parked"}, "park", ORCH),
    _m({"parked"}, {"queued"}, "unpark", OWNER),
    _m({"parked"}, {"queued"}, "goals-changed", WARDEN),
)
EVENTS = frozenset(m.event for m in MOVES)


def allowed(source: str, target: str, event: str, actor: str) -> bool:
    return any(source in m.sources and target in m.targets and m.event == event and actor in m.actors for m in MOVES)


def events(source: str, target: str, actor: str) -> list[str]:
    """The events that may move a task from ``source`` to ``target`` for ``actor``."""
    return sorted({m.event for m in MOVES if source in m.sources and target in m.targets and actor in m.actors})


def move(task: Task, to: str, event: str, actor: str) -> Task:
    """The task with its new status, or Refused naming the move the table does not hold."""
    if not allowed(task.status, to, event, actor):
        source = task.status or "a new task"
        raise Refused("task-status", f"{task.id}: {source} cannot become {to} by {event} for the {actor}")
    return dataclasses.replace(task, status=to)
