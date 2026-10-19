"""SELF_REFLECT.md: the one place the factory writes about itself, for the owner to read. The warden is its only writer.

To every session it is an append-only black box: a session adds a line with `factory reflect` and learns nothing back
(not even whether the same line was already there); only the owner's dashboard reads it, and both files sit in the
warden's own state folder, which no other user may open.

Entries are kept as records in reflections.json and SELF_REFLECT.md is rendered from them after every change, newest
first. The same kind and text within ``reflect.fold_hours`` of an entry bumps that entry's count instead of adding
one. Text loses its newlines, so an entry is always one line the dashboard draws as plain text.

Each entry is open until the owner, or Claude for them, marks it fixed or ignored. One that comes back while it folds
opens again, marked as come back with the note of the fix that did not hold.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.guard import walk_hold
from runtime.guard.wire import REFUSAL_FIELDS
from runtime.shared import lanes, store
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden.context import Context
from runtime.warden.sessions import registry
from runtime.warden.verbs import ORCH, OWNER

log = logging.getLogger(__name__)

KINDS = ("owner-feedback", "miss", "friction", "discovery", "incident", "misroute")
OPEN, FIXED, IGNORED = "open", "fixed", "ignored"
STATES = (OPEN, FIXED, IGNORED)
ORCHESTRATOR_KINDS = frozenset({"owner-feedback", "misroute"})
INCIDENT = "incident"
_FIELDS = {"id", "at", "kind", "who", "where", "text", "repeats", "last", "state", "closed", "note", "came_back"}
ID_PREFIX = "sr"
_lock = threading.Lock()


def records_path(places: Places) -> Path:
    return places.state / "reflections.json"


def _stamp(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def entries(places: Places) -> list[dict[str, Any]]:
    """Every entry, oldest first; ``closed`` is None while an entry is open."""
    raw = store.read_json_or(records_path(places), [])
    if not isinstance(raw, list) or not all(isinstance(e, dict) and set(e) == _FIELDS for e in raw):
        raise store.StoreError(f"{records_path(places)}: every entry needs {', '.join(sorted(_FIELDS))}")
    return raw


def one_line(text: str) -> str:
    return " ".join(text.split())


def _render(records: list[dict[str, Any]]) -> str:
    blocks = []
    for e in reversed(records):
        when = e["at"].replace("T", " ")[:16] + " UTC"
        meta = [e["where"]] if e["where"] else []
        if e["repeats"] > 1:
            meta.append(f"repeated {e['repeats']} times, last {e['last'].replace('T', ' ')[:16]} UTC")
        if e["state"] != OPEN:
            meta.append(f"{e['state']}{': ' + e['note'] if e['note'] else ''}")
        head = f"### {e['id']} · {when} · {e['kind']} · {e['who']}"
        blocks.append("\n".join([head, e["text"], *([" · ".join(meta)] if meta else [])]))
    return "# SELF_REFLECT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def save(places: Places, records: list[dict[str, Any]]) -> None:
    store.write_json(records_path(places), records, mode=0o600)
    store.write_text(places.reflections, _render(records), mode=0o600)


def add(ctx: Context, kind: str, who: str, where: str, text: str) -> dict[str, Any]:
    """Add one entry, or bump the count of the same kind and text written within the fold window; returns it."""
    text = one_line(text)
    now = ctx.clock.now()
    fold = ctx.settings().get_int("reflect.fold_hours") * 3600
    with _lock:
        records = entries(ctx.places)
        same = next((e for e in reversed(records) if e["kind"] == kind and e["text"] == text
                     and now - _stamp(e["at"]) < fold), None)  # fmt: skip
        if same is not None:
            same["repeats"] += 1
            same["last"] = ctx.clock.iso()
            if same["state"] != OPEN:
                same.update(state=OPEN, closed=None, came_back=True)
            entry = same
        else:
            ident = f"{ID_PREFIX}{1 + max((int(e['id'].removeprefix(ID_PREFIX)) for e in records), default=0)}"
            entry = {"id": ident, "at": ctx.clock.iso(), "kind": kind, "who": who, "where": where, "text": text,
                     "repeats": 1, "last": ctx.clock.iso(), "state": OPEN, "closed": None, "note": "",
                     "came_back": False}  # fmt: skip
            records.append(entry)
        save(ctx.places, records)
    return entry


def mark(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The owner's `reflections.mark`: entries become fixed, ignored or open, all of them or, on a bad id, none."""
    ids, state, note = args.get("ids"), args.get("state"), args.get("note", "")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        raise Refused("args", "ids lists the entries to mark, such as sr12")
    if state not in STATES:
        raise Refused("args", f"state is one of {', '.join(STATES)}")
    limit = ctx.settings().get_int("reflect.max_chars")
    if not isinstance(note, str) or len(one_line(note)) > limit:
        raise Refused("args", f"a note is one line of at most {limit} characters")
    with _lock:
        records = entries(ctx.places)
        found = {e["id"]: e for e in records}
        unknown = sorted(set(ids) - set(found))
        if unknown:
            raise Refused("args", f"no Self-reflect entry {', '.join(unknown)}")
        for ident in ids:
            found[ident].update(state=state, closed=None if state == OPEN else ctx.clock.iso(),
                                note="" if state == OPEN else one_line(note), came_back=False)  # fmt: skip
        save(ctx.places, records)
    return ok(marked=len(set(ids)), state=state)


def append(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """`factory reflect --kind <kind> "<text>"`."""
    kind, text = args.get("kind"), args.get("text")
    if kind not in KINDS or kind == INCIDENT:
        allowed = ", ".join(k for k in KINDS if k != INCIDENT)
        raise Refused("reflect", f"--kind must be one of {allowed}")
    if kind in ORCHESTRATOR_KINDS and (ctx.caller.role not in (ORCH, OWNER) or ctx.caller.agent_id):
        raise Refused("reflect", f"only the orchestrator writes {kind}")
    if not isinstance(text, str) or not one_line(text):
        raise Refused("reflect", "say what happened and what would have prevented it, in one or two sentences")
    limit = ctx.settings().get_int("reflect.max_chars")
    if len(one_line(text)) > limit:
        raise Refused("reflect", f"keep it to {limit} characters: one or two sentences")
    who, where = ctx.caller.role, ""
    session = registry.read(ctx.places).get(ctx.caller.session) if ctx.caller.session else None
    if session is not None:
        who = f"{session.role} {session.name}"
        where = f"task {session.task}" if session.task else ""
    if ctx.caller.agent_id:
        who = f"{ctx.caller.agent_kind} subagent of {who}"
    add(ctx, kind, who, where, text)
    return ok(message="Recorded.")


def incident(ctx: Context, text: str, where: str = "") -> None:
    """The warden's own entry: a restart, a rule refusing one session again and again, a revert, an install change."""
    add(ctx, INCIDENT, "warden", where, text)


# The guard's refusals for a spent round limit, by the rounds.* name the limit extends.
SPENT = {"qa-rounds": "qa", "critic-rounds": "critic"}
# The refusals that ask a session only to wait its turn, which ends on its own: no misbehaviour, however often.
WAITS = frozenset({lanes.PLACES, walk_hold.HOLD, walk_hold.UPGRADE})


def _spent(ctx: Context, sid: str, limit: str) -> None:
    """A round limit refused again after the orchestrator's one extension: the task goes to the owner's card."""
    from runtime.queue import cards  # cards reaches this module through the lifecycle

    session = registry.read(ctx.places).get(sid)
    if session is None:
        return
    try:
        cards.spent(ctx, session, limit, f"{session.name} used up its {limit} rounds again")
    except Exception:
        log.exception("warden: putting %s's task on the owner's card failed", sid)


class RefusalWatch:
    """Records each guard refusal, and files an incident when one rule refuses one session or subagent repeatedly, but
    for a wait."""

    def __init__(self) -> None:
        self._recent: dict[tuple[str, str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def record(self, ctx: Context, line: dict[str, Any]) -> None:
        settings = ctx.settings()
        now = ctx.clock.now()
        # The guard's record, as wire names its fields; the warden stamps its own time.
        fields = {k: str(line.get(k, ""))[:2000] for k in REFUSAL_FIELDS}
        store.append_jsonl(ctx.places.refusals, {"at": ctx.clock.iso(), **fields})
        if fields["rule"] in SPENT:
            _spent(ctx, fields["session"], SPENT[fields["rule"]])
        if fields["rule"] in WAITS:
            return
        window = settings.get_int("watch.refusal_window_minutes") * 60
        limit = settings.get_int("watch.refusals_to_notify")
        key = (fields["session"], fields["agent_id"], fields["rule"])
        with self._lock:
            recent = self._recent.setdefault(key, deque())
            # One message's calls, refused at once, are one attempt.
            if recent and now - recent[-1] < settings.get_int("watch.refusal_burst_seconds"):
                return
            recent.append(now)
            while recent and now - recent[0] >= window:
                recent.popleft()
            if len(recent) < limit:
                return
            recent.clear()
        session = registry.read(ctx.places).get(fields["session"])
        who = session.name if session is not None else fields["session"] or "an unregistered session"
        if fields["agent_id"]:
            who = f"{fields['agent_type'] or 'a subagent'} {fields['agent_id'][:8]} of {who}"
        incident(ctx, f"Rule {fields['rule']} refused {who} {limit} times within {window // 60} minutes.")
