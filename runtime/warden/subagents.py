"""Subagents in the registry: the guard reports each start and each hand-back of a session's subagent.

A subagent runs inside its parent's process, so the warden cannot see it from the socket's peer. The guard's
SubagentStart and SubagentStop hooks report it from the parent's process tree, and the guard stamps a subagent's own
`factory` calls with its id; the dispatcher accepts that id only from the parent it was registered under.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

from runtime.shared.models import RETIRED, WORKING, Session
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden import lifecycle, registry
from runtime.warden.context import Caller, Context
from runtime.warden.cost import family_of

_AGENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_KIND = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _agent_id(args: dict[str, Any]) -> str:
    value = args.get("agent_id")
    if not isinstance(value, str) or not _AGENT_ID.match(value):
        raise Refused("args", "agent_id is 1 to 64 letters, digits, - or _")
    return value


def _model(ctx: Context, kind: str) -> tuple[str, str]:
    """The model and effort the guard holds this kind to; ("", "") for a kind with no setting, such as Explore."""
    key = "critic" if kind.startswith("critic-") else kind.replace("-", "_")
    try:
        return ctx.settings().model(key)
    except KeyError:
        return "", ""


def started(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    agent_id = _agent_id(args)
    kind = args.get("kind")
    if not isinstance(kind, str) or not _KIND.match(kind):
        raise Refused("args", "kind is the subagent's type")
    with ctx.lock:
        sessions = lifecycle.read(ctx)
        parent = sessions.get(ctx.caller.session)
        if parent is None or parent.state == RETIRED:
            raise Refused("session", "only a live session the warden started registers a subagent")
        if agent_id in sessions:
            return ok(session=agent_id)
        model, effort = _model(ctx, kind)
        now = ctx.clock.iso()
        sessions[agent_id] = Session(
            id=agent_id, name=f"{parent.name}.{kind}-{agent_id[:6]}", role=kind, family=family_of(kind, parent.family),
            model=model, effort=effort, task=parent.task, parent=parent.id, worktree=parent.worktree,
            state=WORKING, started=now, started_by=parent.id, agent_id=agent_id, since=now,
        )  # fmt: skip
        registry.write(ctx.places, sessions)
    return ok(session=agent_id)


def ended(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """A hand-back: the subagent is retired as finished, unless the owner or a bench already retired it."""
    agent_id = _agent_id(args)
    with ctx.lock:
        sessions = lifecycle.read(ctx)
        row = sessions.get(agent_id)
        if row is None or row.parent != ctx.caller.session:
            raise Refused("session", "no subagent of yours has that id")
        if row.state == RETIRED:
            return ok(session=agent_id, retired=row.retired)
        lifecycle.retire(ctx, [row], row, "finished", lifecycle.WARDEN, "handed back")
        registry.write(ctx.places, sessions)
    return ok(session=agent_id, retired=row.retired)


def acting_as(places: Places, caller: Caller, agent_id: str) -> Caller:
    """The caller as its subagent ``agent_id``: only the parent session it was registered under may name it."""
    if caller.kind != "agent" or not caller.session or not _AGENT_ID.match(agent_id):
        raise Refused("agent", "only a session's own subagent sends an agent id")
    row = registry.read(places).get(agent_id)
    if row is None or row.parent != caller.session:
        raise Refused("agent", "that subagent is not registered under your session")
    if row.state == RETIRED:
        raise Refused("retired", f"{row.name} was {row.retired} and can never act again: hand back")
    return dataclasses.replace(caller, agent_id=agent_id, agent_kind=row.role)
