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
from runtime.warden import cost, launch, lifecycle, permits, registry
from runtime.warden.context import Context
from runtime.warden.launch import LaunchError
from runtime.warden.lifecycle import OWNER
from runtime.warden.permits import CAP, SEVERE

# The agent reads this line first; the thread finds the owner's messages in the transcript by it.
PREFIX = "Message from the owner, sent from the dashboard:\n\n"


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
            parent = sessions.get(s.parent)
            raise Refused("subagent", f"{s.name} runs inside {parent.name if parent else 'its parent'}'s session: "
                                      "message that session")  # fmt: skip
        if s.state == RETIRED:
            raise Refused("retired", f"{s.name} is retired and can never act again")
        if s.state in (models.BENCH_PENDING, models.REVIVING):
            moving = "going to" if s.state == models.BENCH_PENDING else "coming back from"
            raise Refused("settling", f"{s.name} is {moving} the bench; send it again in a minute")
    if s.state == DEPRIORITIZED:
        _bring_back(ctx, s, PREFIX + text)
        return ok(message=f"{s.name} is coming back from the bench with your message.")
    try:
        ctx.launcher.message_session(s.id, PREFIX + text)
    except LaunchError as exc:
        raise Refused("message", f"the message did not reach {s.name}: {exc}") from exc
    return ok(message=f"Sent. {s.name} reads it as soon as its current turn ends.")


def _bring_back(ctx: Context, s: Session, message: str) -> None:
    """Revive a benched session with the owner's message, when a place is free. The owner picks which one comes back,
    so the order the orchestrator keeps is not checked."""
    with ctx.lock:
        if lifecycle.paused_path(ctx).exists():
            raise Refused("paused", "the factory is paused: resume it first, then send the message")
        sessions = lifecycle.read(ctx)
        s = sessions[s.id]
        if s.state != DEPRIORITIZED:
            raise Refused("state", f"{s.name} is {s.state} now; send the message again")
        task = permits.read_task(ctx, s.task) if s.task else None
        permits.check_vitals(ctx, task is None or task.severity == SEVERE)
        if s.role in CAP and task is not None:
            permits.check_cap(ctx.settings(), sessions.values(), s.role)
        lifecycle.move(ctx, s, models.REVIVING, OWNER, "coming back with the owner's message")
        registry.write(ctx.places, sessions)
    failed = lifecycle.bring_back(ctx, s.id, OWNER, note=message)
    if failed:
        raise Refused("revive", f"{s.name} stays on the bench: {failed}")


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
        if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
            continue
        content = record.get("message", {}).get("content")
        at = str(record.get("timestamp", ""))
        if record["type"] == "user" and isinstance(content, str):
            # A prompt of any kind ends the reply before it; only the owner's starts a new entry.
            if content.startswith(PREFIX):
                found.append({"at": at, "text": content.removeprefix(PREFIX), "replies": []})
            elif found:
                found[-1]["closed"] = True
            continue
        if record["type"] == "assistant" and found and not found[-1].get("closed") and isinstance(content, list):
            words = "\n\n".join(c["text"] for c in content if isinstance(c, dict) and c.get("type") == "text")
            if words.strip():
                found[-1]["replies"].append({"at": at, "text": words})
    for entry in found:
        entry.pop("closed", None)
    return found[-settings.get_int("dashboard.thread_messages") :]
