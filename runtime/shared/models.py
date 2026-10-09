"""The records every part of the factory shares: sessions, requests, tasks, approvals, cards, probes and costs.

Each record round-trips through plain JSON. ``load`` refuses a record with a missing or unknown field rather than
filling a default, so a malformed state file fails loudly where it is read.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")

# Lifecycle (factory spec, "Agent lifecycle"): working and waiting hold a cap slot; deprioritized keeps its full
# context; retired is final and can never be resumed.
WORKING, WAITING, DEPRIORITIZED, RETIRED = "working", "waiting", "deprioritized", "retired"
STATES = (WORKING, WAITING, DEPRIORITIZED, RETIRED)
HOLDS_SLOT = (WORKING, WAITING)
FINISHED, STOPPED, WITH_PARENT, CRASHED = "finished", "stopped", "with-parent", "crashed"
RETIRE_KINDS = (FINISHED, STOPPED, WITH_PARENT, CRASHED)

FAMILIES = ("orch", "intake", "design", "impl", "qa")
INBOXES = ("agents", "owner", "users")
TASK_SEVERITIES = ("severe", "major", "minor")
REQUEST_SEVERITIES = ("severe", "normal")
SURFACES = ("ui", "api", "access", "desktop", "none")
# The order a task's status may move in; closed can follow any status.
TASK_STATUSES = (
    "queued", "proposed", "approved", "designing", "building", "deprioritized", "qa-clean", "turn", "merged",
    "checked", "on-staging", "promoted", "closed",
)  # fmt: skip


class RecordError(ValueError):
    """A stored record is missing a field, has an unknown one, or holds a value of the wrong kind."""


def dump(record: Any) -> dict[str, Any]:
    return dataclasses.asdict(record)


def load(cls: type[T], raw: object) -> T:
    if not isinstance(raw, dict):
        raise RecordError(f"{cls.__name__}: not an object")
    names = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    needed = {f.name for f in dataclasses.fields(cls) if f.default is dataclasses.MISSING  # type: ignore[arg-type]
              and f.default_factory is dataclasses.MISSING}  # fmt: skip
    missing, unknown = needed - raw.keys(), raw.keys() - names
    if missing or unknown:
        raise RecordError(f"{cls.__name__}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    return cls(**raw)


@dataclass(slots=True)
class Session:
    """One session or subagent the warden knows: the registry's row."""

    id: str
    name: str
    role: str
    family: str
    model: str
    effort: str
    task: str
    parent: str
    worktree: str
    slot: int | None
    state: str
    started: str
    started_by: str
    doing: str = ""
    retired: str = ""
    retired_by: str = ""
    note: str = ""
    ended: str = ""
    resume_tries: int = 0
    merge_turn: bool = False
    rung: int | None = None
    chat_link: str = ""
    extensions: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class Request:
    id: str
    inbox: str
    from_role: str
    at: str
    severity: str
    where: str
    body: str
    report: str = ""
    outcome: str = ""


@dataclass(slots=True)
class Task:
    id: str
    title: str
    requests: list[str]
    goal_item: str
    severity: str
    surfaces: list[str]
    status: str
    created: str
    body: str
    cause: str = ""
    slices: int | None = None
    owner_class: str = "none"
    score: float | None = None
    run: int | None = None
    why: str = ""
    picture: str = ""
    after: list[str] = field(default_factory=list)
    boosted_at: str = ""
    not_now: bool = False
    branch: str = ""


@dataclass(slots=True)
class Approval:
    task: str
    rung: int
    model: str
    by: str
    at: str


@dataclass(slots=True)
class Card:
    """Something that needs the owner, other than a proposal: a spec's sign-off, a decision, a question."""

    id: str
    kind: str  # spec, decide, question
    task: str
    title: str
    ask: str
    created: str
    picture: str = ""
    brief: str = ""
    comments: int = 0
    answered: str = ""


@dataclass(slots=True)
class Probe:
    at: str
    env: str
    ok: bool
    commit: str
    ms: int | None
    error: str = ""


@dataclass(slots=True)
class CostRecord:
    at: str
    session: str
    subagent: str
    role: str
    family: str
    task: str
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int
    usd: float | None
    failed: bool = False
