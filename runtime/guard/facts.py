"""What the guard knows about one call: the hook input, and what the warden published for it.

The guard runs as the agent user, so it reads only the warden's public folder: the effective settings, the rule
switches, the sessions and the ready records. A file there that cannot be read raises, and the hook refuses the call.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from runtime.shared import settings as settings_module
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.settings import Settings

# A session's registry key until the warden's next tick sees it running; must mirror runtime.warden.launch.PENDING.
PENDING = "pending-"
RETIRED = "retired"  # must mirror runtime.shared.models.RETIRED
UNPERMITTED = "unpermitted"
OWNER = "owner"
ORCHESTRATOR, IMPLEMENTER, DESIGNER = "orchestrator", "implementer", "designer"


@dataclass(frozen=True, slots=True)
class Refusal:
    """A refused call: the rule that refused it and the one line the agent reads, which says what to do."""

    rule: str
    reason: str


@dataclass(frozen=True, slots=True)
class Rewrite:
    """An allowed call whose input the guard changed, such as a shell timeout cut to a tester's time left."""

    updated_input: dict[str, Any]


Decision = Refusal | Rewrite | None


def public(places: Places) -> Path:
    return places.state / "public"


@dataclass(frozen=True, slots=True)
class SessionView:
    """One row of the warden's public sessions file: a session it started, or a subagent under one."""

    id: str
    name: str
    role: str
    family: str
    task: str
    parent: str
    worktree: str
    state: str
    agent_id: str
    retired: str
    extensions: dict[str, int]


_FIELDS = frozenset(f.name for f in dataclasses.fields(SessionView)) - {"id"}


def read_sessions(places: Places) -> dict[str, SessionView]:
    """Every session the warden published, by id; none before it writes the file."""
    path = public(places) / "sessions.json"
    raw = store.read_json_or(path, {})
    if not isinstance(raw, dict):
        raise store.StoreError(f"{path}: not an object")
    out: dict[str, SessionView] = {}
    for sid, row in raw.items():
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise store.StoreError(f"{path}: session {sid} does not have exactly {sorted(_FIELDS)}")
        out[sid] = SessionView(id=sid, **row)
    return out


def read_switches(places: Places) -> frozenset[str]:
    """The rules the owner switched off: ``{"off": [...]}``; none before the warden writes the file."""
    path = public(places) / "switches.json"
    raw = store.read_json_or(path, {"off": []})
    off = raw.get("off") if isinstance(raw, dict) else None
    if not isinstance(off, list) or not all(isinstance(rule, str) for rule in off):
        raise store.StoreError(f"{path}: holds no 'off' list of rule names")
    return frozenset(off)


def read_vitals_level(places: Places) -> str:
    """The machine's vitals level the warden last published; "unknown" before its first reading."""
    raw = store.read_json_or(public(places) / "vitals.json", {"level": "unknown"})
    level = raw.get("level") if isinstance(raw, dict) else None
    if not isinstance(level, str):
        raise store.StoreError(f"{public(places) / 'vitals.json'}: holds no level")
    return level


def read_desktops(places: Places) -> dict[str, str]:
    """Who holds each slot's desktop: slot number to subagent id; none before any tester took one."""
    path = public(places) / "desktops.json"
    raw = store.read_json_or(path, {})
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        raise store.StoreError(f"{path}: not a holder per slot")
    return {str(k): v for k, v in raw.items()}


def read_settings(places: Places) -> Settings:
    """The effective settings the warden published, checked again against the schema."""
    tree = store.read_json(public(places) / "settings.json")
    if not isinstance(tree, dict):
        raise store.StoreError(f"{public(places) / 'settings.json'}: not an object")
    return settings_module.build(tree, {})


def read_ready(places: Places, session_id: str) -> list[dict[str, Any]]:
    """A session's ready records, oldest first: ``{"records": [{"head", "slices", "at"}, ...]}``."""
    path = places.ready(session_id)
    raw = store.read_json_or(path, {"records": []})
    records = raw.get("records") if isinstance(raw, dict) else None
    if not isinstance(records, list) or not all(
        isinstance(r, dict) and isinstance(r.get("head"), str) and isinstance(r.get("at"), str) for r in records
    ):
        raise store.StoreError(f"{path}: holds no list of records with a head and a time")
    return records


def _record_name(home: Path, sid: str) -> str:
    """The name Claude Code's record of the running session ``sid`` holds; "" when there is none."""
    for path in (home / ".claude" / "sessions").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        # telemetry: expected — a record is removed or rewritten as its session ends
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("sessionId") == sid:
            return str(record.get("name") or "")
    return ""


@dataclass(frozen=True, slots=True)
class Call:
    """One hook input, with the warden's published facts beside it."""

    payload: dict[str, Any]
    places: Places
    settings: Settings
    switches: frozenset[str]
    sessions: dict[str, SessionView]
    now: datetime
    home: Path

    @property
    def session(self) -> SessionView | None:
        """The session's row: under its own id or, until the warden binds it, the pending start of the same name.

        `claude --bg` picks the session's id itself, so a start is known only by the name it was given; Claude Code's
        record of the running session pairs that name with the id the hook sees.
        """
        sid = str(self.payload.get("session_id", ""))
        if not sid:
            return None
        found = self.sessions.get(sid)
        if found is not None:
            return found
        name = _record_name(self.home, sid)
        return next((s for s in self.sessions.values()
                     if name and s.id.startswith(PENDING) and s.name == name and s.state != RETIRED), None)  # fmt: skip

    @property
    def role(self) -> str:
        return self.session.role if self.session else UNPERMITTED

    @property
    def tool(self) -> str:
        return str(self.payload.get("tool_name", ""))

    @property
    def tool_input(self) -> dict[str, Any]:
        raw = self.payload.get("tool_input")
        return raw if isinstance(raw, dict) else {}

    @property
    def agent_id(self) -> str:
        return str(self.payload.get("agent_id") or "")

    @property
    def agent_type(self) -> str:
        return str(self.payload.get("agent_type") or "")

    @property
    def in_subagent(self) -> bool:
        return bool(self.agent_id)

    @property
    def transcript(self) -> Path:
        return Path(str(self.payload.get("transcript_path", "")))

    @property
    def cwd(self) -> Path:
        return Path(str(self.payload.get("cwd") or self.worktree))

    @property
    def worktree(self) -> Path:
        """Where this session may write: the orchestrator's is the product's main checkout."""
        if self.role == ORCHESTRATOR or self.session is None or not self.session.worktree:
            return Path(self.settings.get_str("repo.path"))
        return Path(self.session.worktree)

    def off(self, rule: str) -> bool:
        return rule in self.switches

    def extension(self, key: str) -> int:
        """Rounds the owner or the orchestrator added to this session, by the ``rounds.*`` name they extend."""
        return self.session.extensions.get(key, 0) if self.session else 0

    def with_input(self, tool_input: dict[str, Any]) -> Call:
        return dataclasses.replace(self, payload={**self.payload, "tool_input": tool_input})


def resolve(word: str, cwd: Path, home: Path) -> Path:
    """Where a path a command names points: `~` is the home folder, a relative path starts at ``cwd``."""
    if word == "~" or word.startswith("~/"):
        word = str(home) + word[1:]
    return Path(os.path.normpath(cwd / word))


def inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents
