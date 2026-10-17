"""The warden's registry of sessions: one JSON file only the warden writes, and the guard's view of it in public/.

Reading returns every session keyed by id; a malformed file raises StoreError (the caller refuses what needs it).
Lifecycle moves live in lifecycle.py; this module only stores and queries.
"""

from __future__ import annotations

from collections.abc import Iterable

from runtime.shared import models, store
from runtime.shared.models import HOLDS_SLOT, RETIRED, TESTER, Session
from runtime.shared.paths import Places

# contract: rows written before a slot's lease file became the one record of its holder carry a slot of their own.
# Dropped on read, so the warden's first write at its start leaves none; this line goes in the release after.
_DROPPED = ("slot",)


def read(places: Places) -> dict[str, Session]:
    raw = store.read_json_or(places.registry, {})
    if not isinstance(raw, dict):
        raise store.StoreError(f"{places.registry}: not an object")
    rows = {sid: {k: v for k, v in row.items() if k not in _DROPPED} if isinstance(row, dict) else row
            for sid, row in raw.items()}  # fmt: skip
    return {sid: models.load(Session, row) for sid, row in rows.items()}


def write(places: Places, sessions: dict[str, Session]) -> None:
    store.write_json(places.registry, {sid: models.dump(s) for sid, s in sessions.items()}, mode=0o600)
    # The guard reads only what it needs, from public/: no task text, no chat links.
    # contract: extensions, kinds and started_as added, for the guard's round limits, its lanes and its browser rule.
    public = {
        sid: {k: getattr(s, k) for k in ("name", "role", "family", "task", "parent", "worktree", "state", "agent_id",
                                        "retired", "extensions", "started", "kinds", "started_as")}
        for sid, s in sessions.items()
    }  # fmt: skip
    store.write_json(places.state / "public" / "sessions.json", public)


def live(sessions: Iterable[Session]) -> list[Session]:
    return [s for s in sessions if s.state != RETIRED]


def holding(sessions: Iterable[Session], task_id: str) -> Session | None:
    """The run on ``task_id`` that still holds its place, as one does when a facts change voids its approval."""
    return next((s for s in sessions if s.task == task_id and s.state in HOLDS_SLOT and not s.parent), None)


def seated(sessions: Iterable[Session], role: str) -> list[Session]:
    """Sessions of ``role`` that hold a cap slot: working, waiting, or between states."""
    return [s for s in sessions if s.role == role and s.state in HOLDS_SLOT and not s.parent]


def children(sessions: dict[str, Session], parent_id: str) -> list[Session]:
    return [s for s in sessions.values() if s.parent == parent_id]


def testing(sessions: Iterable[Session], parent_id: str) -> list[Session]:
    """The testers session ``parent_id`` started that have not handed back."""
    return [s for s in sessions if s.parent == parent_id and s.role.startswith(TESTER) and s.state != RETIRED]


def subtree(sessions: dict[str, Session], root_id: str) -> list[Session]:
    out, todo = [], [root_id]
    while todo:
        sid = todo.pop()
        if sid in sessions:
            out.append(sessions[sid])
            todo += [c.id for c in children(sessions, sid)]
    return out


def following(places: Places, sessions: dict[str, Session], sid: str) -> str | None:
    """The id session ``sid`` is under now: its own, or where the history moved it (a pending id to its session, a
    session to its copy); None when no row holds it."""
    if sid in sessions:
        return sid
    moved = {str(row["was"]): str(row["session"]) for row in store.read_jsonl(places.history) if "was" in row}
    seen: set[str] = set()
    while sid not in sessions and sid in moved and sid not in seen:
        seen.add(sid)
        sid = moved[sid]
    return sid if sid in sessions else None


def by_name(sessions: Iterable[Session], name: str) -> Session | None:
    return next((s for s in sessions if s.name == name and s.state != RETIRED), None)
