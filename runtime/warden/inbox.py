"""The orchestrator's inbox: every event that should start one of its loop passes.

The warden only appends to inbox.jsonl; a cursor file keeps the last event handed over and the last one the
orchestrator acted on. ``factory inbox --wait`` first acknowledges the events the previous wait handed over (the
orchestrator runs it again once it acted on them), then blocks until an event arrives or watch.inbox_wait_seconds
pass with none; the command then asks again. ``factory inbox`` lists every event not acknowledged yet, so after a
restart, or a background wait whose answer was lost, nothing is missed.
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
NEEDS_RUNG = "needs rung"  # an implementer spent routing.cause_tries without confirming a cause
# Every owner decision (plan R4), so the orchestrator acts at once; `answered` carries the answer to a question only
# the orchestrator can act on (R15): the warden hands a session's own answer to it directly.
APPROVED, NOT_NOW, DROPPED, BOOSTED, GO = "approved", "not-now", "dropped", "boosted", "go"
DECIDED_EXTEND, LIMIT_EXTENDED, TO_DESIGNER, PARK = "decided-extend", "limit-extended", "to-designer", "park"
STOPPED, PAUSED, RESUMED, SLOT_FREE = "stopped", "paused", "resumed", "proposal-slot-free"
ANSWERED, ORCHESTRATOR_STARTED = "answered", "orchestrator-started"
OWNER_DECISIONS = (
    APPROVED, NOT_NOW, DROPPED, BOOSTED, GO, DECIDED_EXTEND, LIMIT_EXTENDED, TO_DESIGNER, PARK, STOPPED, PAUSED,
    RESUMED, SLOT_FREE, GOAL_CHANGED, ANSWERED, ORCHESTRATOR_STARTED,
)  # fmt: skip
KINDS = frozenset(
    {
        LOOP_DUE,
        STAGING_RED,
        PROD_RED,
        SEVERE_REQUEST,
        TRIAGE_DUE,
        QA_PROD_DUE,
        BENCHED,
        CRASHED,
        NEEDS_RUNG,
        *OWNER_DECISIONS,
    }
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
    """Whether an event of ``kind`` waits unacknowledged, so the loop never piles up copies of one reminder."""
    with store.locked(_lock(places)):
        acked = _read_cursor(places)["acked"]
        return any(r["kind"] == kind for r in store.read_jsonl(places.inbox) if r["seq"] > acked)


def unacknowledged(places: Places) -> list[dict[str, Any]]:
    """Every event the orchestrator has not acted on yet, oldest first."""
    with store.locked(_lock(places)):
        acked = _read_cursor(places)["acked"]
        return [r for r in store.read_jsonl(places.inbox) if r["seq"] > acked]


def _read_cursor(places: Places) -> dict[str, int]:
    raw = store.read_json_or(_cursor(places), {"handed": 0, "acked": 0})
    if not isinstance(raw, dict) or not all(isinstance(raw.get(k), int) for k in ("handed", "acked")):
        raise store.StoreError(f"{_cursor(places)}: want the handed and acked counts")
    return {"handed": raw["handed"], "acked": raw["acked"]}


def _write_cursor(places: Places, clock: Clock, handed: int, acked: int) -> None:
    store.write_json(_cursor(places), {"handed": handed, "acked": acked, "at": clock.iso()}, mode=0o600)


def _hand(places: Places, clock: Clock, after: str) -> list[dict[str, Any]]:
    """The events after the handed (``after="handed"``) or the acked cursor, now handed over."""
    with store.locked(_lock(places)):
        cursor = _read_cursor(places)
        events = [r for r in store.read_jsonl(places.inbox) if r["seq"] > cursor[after]]
        if events and events[-1]["seq"] > cursor["handed"]:
            _write_cursor(places, clock, events[-1]["seq"], cursor["acked"])
    return events


def acknowledge(places: Places, clock: Clock) -> None:
    """Everything handed over so far was acted on: the next wait, or the end of a loop pass, says so."""
    with store.locked(_lock(places)):
        cursor = _read_cursor(places)
        if cursor["acked"] < cursor["handed"]:
            _write_cursor(places, clock, cursor["handed"], cursor["handed"])


def _answer(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return ok(events=[], message="nothing new")
    return ok(events=events, message="\n".join(f"{e['at']} {e['kind']}" for e in events))


def read(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Every event not acknowledged yet, oldest first: what a restarted orchestrator still owes."""
    return _answer(_hand(ctx.places, ctx.clock, "acked"))


def wait(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Acknowledge what was handed over, then block until a new event arrives or watch.inbox_wait_seconds pass."""
    acknowledge(ctx.places, ctx.clock)
    end = time.monotonic() + ctx.settings().get_int("watch.inbox_wait_seconds")
    with _posted:
        while not (events := _hand(ctx.places, ctx.clock, "handed")) and (left := end - time.monotonic()) > 0:
            _posted.wait(left)
    return _answer(events)
