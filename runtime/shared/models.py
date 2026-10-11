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
# Written before the warden acts, so the crash watch and the backstop never misread a session in between.
BENCH_PENDING, REVIVING = "bench-pending", "reviving"
STATES = (WORKING, WAITING, BENCH_PENDING, DEPRIORITIZED, REVIVING, RETIRED)
HOLDS_SLOT = (WORKING, WAITING, BENCH_PENDING, REVIVING)
FINISHED, STOPPED, WITH_PARENT, CRASHED = "finished", "stopped", "with-parent", "crashed"
RETIRE_KINDS = (FINISHED, STOPPED, WITH_PARENT, CRASHED)

FAMILIES = ("orch", "intake", "design", "impl", "qa")
INBOXES = ("agents", "owner", "users")
TASK_SEVERITIES = ("severe", "major", "minor")
REQUEST_SEVERITIES = ("severe", "normal")
SURFACES = ("ui", "api", "access", "desktop", "none")
# Every task status. Moves follow runtime/queue/statuses.py's table, never "forward" alone.
TASK_STATUSES = (
    "queued", "parked", "proposed", "approved", "designing", "review", "building", "deprioritized", "qa",
    "qa-clean", "turn", "merged", "checked", "on-staging", "promoted", "decide", "closed",
)  # fmt: skip
CARD_KINDS = ("spec", "decide", "question")


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
    state: str
    started: str
    started_by: str
    doing: str = ""
    retired: str = ""
    retired_by: str = ""
    note: str = ""
    ended: str = ""
    resume_tries: int = 0  # resumes after crashes within the crash window
    crashed_at: str = ""  # when the crash watch last resumed it
    merge_turn: bool = False
    rung: int | None = None
    chat_link: str = ""
    agent_id: str = ""  # a subagent's id in its parent's transcript
    # contract: when the session entered its state; the bench grace and a pending start's grace count from it.
    since: str = ""
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
    digest: str = ""  # contract: R20, the hash of the title, rung, model and picture the owner approved


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
    asked_by: str = ""  # contract: R15, the session a question's answer goes back to
    comments_at: str = ""  # contract: R14, when the designer last counted the comments
    limit: str = ""  # a decide card's spent limit, by its rounds.* name: what the owner's Extend raises


@dataclass(slots=True)
class Outage:
    """A clear prod outage and the one bypass run it allows (routing spec)."""

    since: str
    stage: str  # down, fixing, on-staging, stopped
    task: str = ""
    session: str = ""
    bypass_used: bool = False


@dataclass(slots=True)
class Operation:
    """A long owner action whose steps the dashboard shows, surviving a reload."""

    id: str
    kind: str
    started: str
    steps: list[dict[str, str]]  # each {"text": ..., "state": "waiting|running|done|failed", "detail": ...}
    state: str  # running, done, failed
    result: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Probe:
    at: str
    env: str
    ok: bool
    commit: str
    ms: int | None
    error: str = ""
    # contract: added by 1B-2. A failure that counts toward an outage: the host itself answered with an HTTP error
    # or timed out, and the control probe (GitHub) answered. A network failure on this side never counts.
    counts: bool = False


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
