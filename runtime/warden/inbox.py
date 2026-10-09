"""The orchestrator's inbox: every event that should start one of its loop passes.

The warden only appends to inbox.jsonl; a cursor file keeps the last event handed over, so a read returns only
what is new and marks it delivered. ``factory inbox --wait`` blocks here until an event arrives or
watch.inbox_wait_seconds pass with none; the command then asks again.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.protocol import ok
from runtime.warden.context import Clock, Context

LOOP_DUE = "loop due"
STAGING_RED = "staging red"
PROD_RED = "prod red"
SEVERE_REQUEST = "severe request"
TRIAGE_DUE = "triage due"
QA_PROD_DUE = "qa-prod due"  # prod answers on a build the owner pushed: walk it read-only
# The crash watch: a run benched for higher work, or retired after its process kept ending.
BENCHED, CRASHED = "benched", "crashed"
GOAL_CHANGED = "goal-changed"  # the loop read a new GOAL.md: the scores changed, so the proposals may too
# Every owner decision (plan R4), so the orchestrator acts at once; `answered` carries a question's answer (R15).
OWNER_DECISIONS = (
    "approved", "not-now", "dropped", "boosted", "go", "feedback", "extend", "to-designer", "park", "stopped",
    "paused", "resumed", "proposal-slot-free", "goal-changed", "answered", "orchestrator-started",
)  # fmt: skip
KINDS = frozenset(
    {LOOP_DUE, STAGING_RED, PROD_RED, SEVERE_REQUEST, TRIAGE_DUE, QA_PROD_DUE, BENCHED, CRASHED, *OWNER_DECISIONS}
)

# One condition for every inbox in the process: a waiter re-reads its own file when woken.
_posted = threading.Condition()


def _cursor(places: Places) -> Path:
    return places.state / "inbox-cursor.json"


def _lock(places: Places) -> Path:
    return places.state / ".inbox.lock"


def post_event(places: Places, clock: Clock, kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """Append one event and wake every waiter; the stored record."""
    if kind not in KINDS:
        raise ValueError(f"{kind!r} is not an inbox event")
    with store.locked(_lock(places)):
        records = store.read_jsonl(places.inbox)
        record = {"seq": records[-1]["seq"] + 1 if records else 1, "at": clock.iso(), "kind": kind, "data": data}
        store.append_jsonl(places.inbox, record)
    with _posted:
        _posted.notify_all()
    return record


def poster(places: Places, clock: Clock) -> Callable[[str, dict[str, Any]], None]:
    """What wardend gives every Context as ``post_event``."""

    def post(kind: str, data: dict[str, Any]) -> None:
        post_event(places, clock, kind, data)

    return post


def pending(places: Places, kind: str) -> bool:
    """Whether an event of ``kind`` waits undelivered, so the loop never piles up copies of one reminder."""
    with store.locked(_lock(places)):
        return any(r["kind"] == kind for r in _undelivered(places))


def _undelivered(places: Places) -> list[dict[str, Any]]:
    delivered = store.read_json_or(_cursor(places), {"delivered": 0})
    if not isinstance(delivered, dict) or not isinstance(delivered.get("delivered"), int):
        raise store.StoreError(f"{_cursor(places)}: no delivered count")
    return [r for r in store.read_jsonl(places.inbox) if r["seq"] > delivered["delivered"]]


def _take(places: Places, clock: Clock) -> list[dict[str, Any]]:
    with store.locked(_lock(places)):
        events = _undelivered(places)
        if events:
            store.write_json(_cursor(places), {"delivered": events[-1]["seq"], "at": clock.iso()}, mode=0o600)
    return events


def _answer(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return ok(events=[], message="nothing new")
    return ok(events=events, message="\n".join(f"{e['at']} {e['kind']}" for e in events))


def read(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Every undelivered event, oldest first, now marked delivered."""
    return _answer(_take(ctx.places, ctx.clock))


def wait(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Like read, but blocks until an event arrives or watch.inbox_wait_seconds pass with none."""
    end = time.monotonic() + ctx.settings().get_int("watch.inbox_wait_seconds")
    with _posted:
        while not (events := _take(ctx.places, ctx.clock)) and (left := end - time.monotonic()) > 0:
            _posted.wait(left)
    return _answer(events)
