"""The owner's messages to a running session, from the dashboard, and the thread read back from its transcript.

Claude Code delivers a message as the session's next user turn: at once when it is idle, after its current turn when
it is busy. So it carries the owner's own authority, and nothing is stored here: the thread is the transcript.
A benched session comes back with the message; a subagent runs inside its parent's session and has no turn of its own.
"""

from __future__ import annotations

import json
from typing import Any

from runtime.shared import models
from runtime.shared.models import DEPRIORITIZED, RETIRED, Session
from runtime.shared.protocol import Refused, ok
from runtime.warden.context import Context
from runtime.warden.owner import ops
from runtime.warden.sessions import launch, lifecycle, permits, registry, revive
from runtime.warden.sessions.launch import LaunchError
from runtime.warden.sessions.lifecycle import OWNER
from runtime.warden.sessions.permits import CAP, SEVERE
from runtime.warden.spend import cost

# The agent reads this line first; the thread finds the owner's messages in the transcript by it.
PREFIX = "Message from the owner, sent from the dashboard:\n\n"
KIND = "message"  # the operation that delivers one


def _text(ctx: Context, args: dict[str, Any]) -> str:
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        raise Refused("args", "write the message first")
    limit = ctx.settings().get_int("dashboard.message_max_chars")
    if len(text) > limit:
        raise Refused("too-long", f"a message holds at most {limit} characters; this one has {len(text)}")
    return text.strip()


def send(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    text = _text(ctx, args)
    session = args.get("session")
    if not isinstance(session, str) or not session:
        raise Refused("args", "name the session")
    with ctx.lock:
        sessions = lifecycle.read(ctx)
        s = lifecycle.find(ctx, sessions, session)
        if s.parent:
            raise Refused("subagent", "a subagent runs inside its parent's session: message that session")
        if s.state == RETIRED:
            raise Refused("retired", "this session is retired and can never act again")
        if s.state in (models.BENCH_PENDING, models.REVIVING):
            moving = "going to" if s.state == models.BENCH_PENDING else "coming back from"
            raise Refused("settling", f"this session is {moving} the bench; send it again in a minute")
    if s.state == DEPRIORITIZED:
        _to_revive(ctx, s)
        step = ("Bringing it back with your message", lambda: _revive(ctx, s.id, PREFIX + text))
        return ok(op=ops.launch(ctx, KIND, [step], {"revived": True}))
    return ok(op=ops.launch(ctx, KIND, [("Sending your message", lambda: _deliver(ctx, s.id, PREFIX + text))]))


def _deliver(ctx: Context, session_id: str, text: str) -> str:
    """Typing the message in holds the session for watch.message_seconds, longer than the dashboard waits for the
    warden's answer: so it runs as an operation the page follows."""
    try:
        ctx.launcher.message_session(session_id, text)
    except LaunchError as exc:
        raise ops.StepError(f"the message did not reach the session: {exc}") from exc
    return "delivered"


def _to_revive(ctx: Context, s: Session) -> None:
    """Mark a benched session reviving with the owner's message, when a place is free. The owner picks which one comes
    back, so the order the orchestrator keeps is not checked."""
    with ctx.lock:
        if lifecycle.paused_path(ctx).exists():
            raise Refused("paused", "the factory is paused: resume it first, then send the message")
        sessions = lifecycle.read(ctx)
        s = sessions[s.id]
        if s.state != DEPRIORITIZED:
            raise Refused("state", f"this session is {s.state} now; send the message again")
        task = permits.read_task(ctx, s.task) if s.task else None
        permits.check_vitals(ctx, task is None or task.severity == SEVERE)
        permits.check_daemon(ctx)
        if s.role in CAP and task is not None:
            permits.check_cap(ctx.settings(), sessions.values(), s.role)
        lifecycle.move(ctx, s, models.REVIVING, OWNER, "coming back with the owner's message")
        registry.write(ctx.places, sessions)


def _revive(ctx: Context, session_id: str, message: str) -> str:
    failed = revive.bring_back(ctx, session_id, OWNER, note=message)
    if failed:
        raise Refused("revive", f"it stays on the bench: {failed}")
    return "back with your message"


def thread(ctx: Context, s: Session) -> list[dict[str, Any]]:
    """The owner's last messages to ``s``, oldest first, each with what the agent said after it, in its own words.

    Read from the end of the transcript only (dashboard.thread_tail_kib): an older message has left the thread.
    """
    settings = ctx.settings()
    path = cost.transcript([launch.agent_home() / ".claude" / "projects"], s)
    if path is None:
        return []
    tail = settings.get_int("dashboard.thread_tail_kib") * 1024
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - tail))
        raw = handle.read()
    lines = raw.split(b"\n")[1:] if size > tail else raw.split(b"\n")  # the first line may start mid-record
    found: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        # telemetry: expected — the last line is still being written
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") in ("user", "assistant"):
            _add(found, record)
    for entry in found:
        entry.pop("closed", None)
    return found[-settings.get_int("dashboard.thread_messages") :]


def words(content: list[Any]) -> str:
    """The text an assistant record's content says, its tool calls and thinking left out."""
    return "\n\n".join(c["text"] for c in content if isinstance(c, dict) and c.get("type") == "text")


def _add(found: list[dict[str, Any]], record: dict[str, Any]) -> None:
    """One transcript record into the thread: the owner's message starts an entry, and the agent's words after it,
    until the next prompt, are its replies."""
    content = record.get("message", {}).get("content")
    at = str(record.get("timestamp", ""))
    if record["type"] == "user" and isinstance(content, str):
        # A prompt of any kind ends the reply before it; only the owner's starts a new entry.
        if content.startswith(PREFIX):
            found.append({"at": at, "text": content.removeprefix(PREFIX), "replies": []})
        elif found:
            found[-1]["closed"] = True
        return
    if record["type"] == "assistant" and found and not found[-1].get("closed") and isinstance(content, list):
        said = words(content)
        if said.strip():
            found[-1]["replies"].append({"at": at, "text": said})
