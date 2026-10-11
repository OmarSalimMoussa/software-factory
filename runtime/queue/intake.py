"""Requests arriving in the three inboxes, and the owner's boost of a waiting request (intake spec).

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

SEVERE_REQUEST = "severe request"


def body_text(data: bytes | None, what: str, limit_kib: int) -> str:
    """Uploaded text, checked: present, within the upload cap and UTF-8."""
    if not data or not data.strip():
        raise Refused("body", f"the {what} is empty: give it on stdin with a heredoc or a redirect")
    if len(data) > limit_kib * 1024:
        raise Refused("too-large", f"the {what} is larger than queue.max_upload_kib ({limit_kib} KiB)")
    try:
        return data.decode()
    except UnicodeDecodeError as exc:
        raise Refused("body", f"the {what} is not UTF-8 text") from exc


def file_request(
    ctx: Context, queue: Queue, inbox: str, from_role: str, severity: str, where: str, body: str, report: str = ""
) -> Request:
    """Write one new request; the caller holds the queue lock."""
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
        "",
    )
    queue.write({request_rel(req): formats.dump_request(req)})
    if severity == "severe":
        ctx.post_event(SEVERE_REQUEST, {"request": req.id, "inbox": inbox, "where": req.where})
    return req


def boosts(ctx: Context) -> dict[str, str]:
    """Requests the owner boosted, by id, with when; triage takes each into a batch of its own."""
    raw = store.read_json_or(ctx.places.state / "request-boosts.json", {})
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        raise store.StoreError(f"{ctx.places.state / 'request-boosts.json'}: not an object of times")
    return raw


def set_boosts(ctx: Context, found: dict[str, str]) -> None:
    store.write_json(ctx.places.state / "request-boosts.json", found, mode=0o600)
