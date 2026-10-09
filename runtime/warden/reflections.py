"""SELF_REFLECT.md: the one place the factory writes about itself, for the owner to read. The warden is its only writer.

Entries are kept as records in reflections.json and SELF_REFLECT.md is rendered from them after every change, newest
first. The same kind and text within ``reflect.fold_hours`` of an entry bumps that entry's count instead of adding
one. Text loses its newlines, so an entry is always one line the dashboard draws as plain text.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import registry
from runtime.warden.context import Context
from runtime.warden.verbs import ORCH, OWNER

KINDS = ("owner-feedback", "miss", "friction", "discovery", "incident", "misroute")
ORCHESTRATOR_KINDS = frozenset({"owner-feedback", "misroute"})
INCIDENT = "incident"
_FIELDS = {"at", "kind", "who", "where", "text", "repeats", "last"}
_lock = threading.Lock()


def _records_path(places: Places) -> Path:
    return places.state / "reflections.json"


def _stamp(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def entries(places: Places) -> list[dict[str, Any]]:
    """Every entry, oldest first: at, kind, who, where, text, repeats, last."""
    raw = store.read_json_or(_records_path(places), [])
    if not isinstance(raw, list) or not all(isinstance(e, dict) and set(e) == _FIELDS for e in raw):
        raise store.StoreError(f"{_records_path(places)}: every entry needs {', '.join(sorted(_FIELDS))}")
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
        blocks.append(
            "\n".join([f"### {when} · {e['kind']} · {e['who']}", e["text"], *([" · ".join(meta)] if meta else [])])
        )
    return "# SELF_REFLECT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


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
            entry = same
        else:
            entry = {"at": ctx.clock.iso(), "kind": kind, "who": who, "where": where, "text": text, "repeats": 1,
                     "last": ctx.clock.iso()}  # fmt: skip
            records.append(entry)
        store.write_json(_records_path(ctx.places), records, mode=0o600)
        store.write_text(ctx.places.reflections, _render(records))
    return entry


def append(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """`factory reflect --kind <kind> "<text>"`."""
    kind, text = args.get("kind"), args.get("text")
    if kind not in KINDS or kind == INCIDENT:
        allowed = ", ".join(k for k in KINDS if k != INCIDENT)
        raise Refused("reflect", f"--kind must be one of {allowed}")
    if kind in ORCHESTRATOR_KINDS and ctx.caller.role not in (ORCH, OWNER):
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
    entry = add(ctx, kind, who, where, text)
    return {"repeats": entry["repeats"]}


def incident(ctx: Context, text: str, where: str = "") -> None:
    """The warden's own entry: a restart, a rule refusing one session again and again, a revert, an install change."""
    add(ctx, INCIDENT, "warden", where, text)


class RefusalWatch:
    """Records each guard refusal and files an incident when one rule refuses the same session again and again."""

    def __init__(self) -> None:
        self._recent: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def record(self, ctx: Context, line: dict[str, Any]) -> None:
        settings = ctx.settings()
        now = ctx.clock.now()
        # The guard's record: session, rule, tool and detail; the warden stamps its own time.
        fields = {k: str(line.get(k, ""))[:2000] for k in ("session", "rule", "tool", "detail")}
        store.append_jsonl(ctx.places.refusals, {"at": ctx.clock.iso(), **fields})
        window = settings.get_int("watch.refusal_window_minutes") * 60
        limit = settings.get_int("watch.refusals_to_notify")
        key = (fields["session"], fields["rule"])
        with self._lock:
            recent = self._recent.setdefault(key, deque())
            recent.append(now)
            while recent and now - recent[0] >= window:
                recent.popleft()
            if len(recent) < limit:
                return
            recent.clear()
        session = registry.read(ctx.places).get(fields["session"])
        who = session.name if session is not None else fields["session"] or "an unregistered session"
        incident(ctx, f"Rule {fields['rule']} refused {who} {limit} times within {window // 60} minutes.")
