"""The warden's registry of sessions: one JSON file only the warden writes, and the guard's view of it in public/.

Reading returns every session keyed by id; a malformed file raises StoreError (the caller refuses what needs it).
Lifecycle moves live in lifecycle.py; this module only stores and queries.
"""

from __future__ import annotations

from collections.abc import Iterable

from runtime.shared import models, store
from runtime.shared.models import HOLDS_SLOT, RETIRED, Session
from runtime.shared.paths import Places


def read(places: Places) -> dict[str, Session]:
    raw = store.read_json_or(places.registry, {})
    if not isinstance(raw, dict):
        raise store.StoreError(f"{places.registry}: not an object")
    return {sid: models.load(Session, row) for sid, row in raw.items()}


def write(places: Places, sessions: dict[str, Session]) -> None:
    store.write_json(places.registry, {sid: models.dump(s) for sid, s in sessions.items()}, mode=0o600)
    # The guard reads only what it needs, from public/: no task text, no chat links.
    public = {
        sid: {k: getattr(s, k) for k in ("name", "role", "family", "task", "parent", "worktree", "slot", "state",
                                        "agent_id", "retired")}
        for sid, s in sessions.items()
    }  # fmt: skip
    store.write_json(places.state / "public" / "sessions.json", public)


def live(sessions: Iterable[Session]) -> list[Session]:
    return [s for s in sessions if s.state != RETIRED]


def seated(sessions: Iterable[Session], role: str) -> list[Session]:
    """Sessions of ``role`` that hold a cap slot: working, waiting, or between states."""
    return [s for s in sessions if s.role == role and s.state in HOLDS_SLOT and not s.parent]


def children(sessions: dict[str, Session], parent_id: str) -> list[Session]:
    return [s for s in sessions.values() if s.parent == parent_id]


def subtree(sessions: dict[str, Session], root_id: str) -> list[Session]:
    out, todo = [], [root_id]
    while todo:
        sid = todo.pop()
        if sid in sessions:
            out.append(sessions[sid])
            todo += [c.id for c in children(sessions, sid)]
    return out


def by_name(sessions: Iterable[Session], name: str) -> Session | None:
    return next((s for s in sessions if s.name == name and s.state != RETIRED), None)
