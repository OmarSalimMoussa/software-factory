"""Requests arriving in the inboxes, the owner's boost of a waiting request (intake spec), and the tasks back from
parked that triage judges again.

A request is written once; only its outcome is added later. A severe one posts ``severe request`` at once; the warden's
tick posts ``triage due`` when a batch is due (runtime/queue/triage.py names it).
"""

from __future__ import annotations

from runtime.queue import formats
from runtime.queue.book import Queue, request_rel
from runtime.shared import store
from runtime.shared.models import Request
from runtime.shared.protocol import Refused
from runtime.warden.context import Context
from runtime.warden.inbox import SEVERE_REQUEST


def body_text(data: bytes | None, what: str, limit_kib: int) -> str:
    """Uploaded text, checked: present, within the upload cap and UTF-8."""
    if not data or not data.strip():
        raise Refused("body", f"the {what} is empty: write it to a file and pass it with --body <file>")
    if len(data) > limit_kib * 1024:
        raise Refused("too-large", f"the {what} is larger than queue.max_upload_kib ({limit_kib} KiB)")
    try:
        return data.decode()
    except UnicodeDecodeError as exc:
        raise Refused("body", f"the {what} is not UTF-8 text") from exc


def file_request(
    ctx: Context,
    queue: Queue,
    inbox: str,
    from_role: str,
    severity: str,
    where: str,
    body: str,
    report: str = "",
    outcome: str = "",
) -> Request:
    """Write one new request, settled at once with ``outcome`` when it is given; the caller holds the queue lock."""
    now = ctx.clock.iso()
    req = Request(
        queue.next_request_id(now[:10]),
        inbox,
        from_role,
        now,
        severity,
        formats.one_line(where),
        body.strip(),
        report,
        outcome,
    )
    queue.write({request_rel(req): formats.dump_request(req)})
    if severity == "severe" and not outcome:
        ctx.post_event(SEVERE_REQUEST, {"request": req.id, "inbox": inbox, "where": req.where})
    return req


# Each by id with when: the requests the owner boosted, which triage takes at once in a batch of their own; and the
# tasks back from parked when the goal items changed, which triage judges before one can be proposed.
BOOSTED, RETURNED = "request-boosts.json", "returned-tasks.json"


def marked(ctx: Context, kind: str) -> dict[str, str]:
    path = ctx.places.state / kind
    raw = store.read_json_or(path, {})
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        raise store.StoreError(f"{path}: not an object of times")
    return raw


def mark(ctx: Context, kind: str, found: dict[str, str]) -> None:
    store.write_json(ctx.places.state / kind, found, mode=0o600)
