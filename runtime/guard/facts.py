"""What the guard knows about one call: the hook input, and what the warden published for it.

The guard runs as the agent user, so it reads only the warden's public folder: the effective settings, the rule
switches, the sessions, the vitals and the ready records. A file there that cannot be read raises, and the hook
refuses the call.
"""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from runtime.shared import lanes, store
from runtime.shared import settings as settings_module
from runtime.shared.models import PENDING, RETIRED
from runtime.shared.paths import Places
from runtime.shared.settings import Settings

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
    started: str  # when it started, as the warden recorded it
    kinds: list[str]
    started_as: str


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


@dataclass(frozen=True, slots=True)
class Vitals:
    """The vitals the warden last published: the level, the readings that set it, the machine's average CPU use in %
    of every CPU (None when it could not read it), and when it read them (None before its first reading)."""

    level: str
    why: str
    cpu: float | None
    at: datetime | None


def read_vitals(places: Places) -> Vitals:
    path = public(places) / "vitals.json"
    raw = store.read_json_or(path, None)
    if raw is None:
        return Vitals("unknown", "none read yet", None, None)
    fields = raw if isinstance(raw, dict) else {}
    level, why, cpu, at = (fields.get(key) for key in ("level", "why", "cpu", "at"))
    number = cpu is None or isinstance(cpu, int | float) and not isinstance(cpu, bool)
    if not (isinstance(level, str) and isinstance(why, str) and number and isinstance(at, str)):
        raise store.StoreError(f"{path}: holds no level, readings, CPU use and time")
    try:
        when = datetime.fromisoformat(at)
    except ValueError as exc:
        raise store.StoreError(f"{path}: its time {at!r} is not one") from exc
    return Vitals(level, why, None if cpu is None else float(cpu), when)


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


# A ready record's mark: its testers reproduce a bug before its fix, and are no QA.
REPRODUCE = "reproduce"
# A ready record's mark: a tester walked its head to a verdict, clean or not. Only such a head is a QA round: one whose
# testers a bench, a pause or a lost slot cut short, or that each tester could not walk, is none.
WALKED = "walked"


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


def qa_round(records: list[dict[str, Any]], head: str) -> int:
    """The QA round testers on ``head`` are in: one for each other head a tester walked to a verdict, then this one."""
    walked = {str(r["head"]) for r in records if r.get(WALKED) and not r.get(REPRODUCE)}
    return len(walked - {head}) + 1


def ready_at(records: list[dict[str, Any]], when: datetime) -> dict[str, Any] | None:
    """The ready record a tester started at ``when`` walks: the newest by then."""
    began = [r for r in records if datetime.fromisoformat(r["at"].replace("Z", "+00:00")) <= when]
    return began[-1] if began else None


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
        return lanes.kind_of(str(self.payload.get("agent_type") or ""))

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
