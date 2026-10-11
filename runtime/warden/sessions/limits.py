"""A session's limits on qa passes, critic rounds and check runs, and one more allowance of a limit: the orchestrator's
once per session and limit, the owner's any time.
"""

from __future__ import annotations

from typing import Any

from runtime.shared.models import RETIRED, Session
from runtime.shared.protocol import Refused, ok
from runtime.warden import inbox
from runtime.warden.context import Context
from runtime.warden.sessions import registry
from runtime.warden.sessions.lifecycle import OWNER, find, read, text_arg
from runtime.warden.sessions.permits import actor

LIMITS = ("qa", "critic", "check_runs")


def extended_once(s: Session, limit: str) -> bool:
    """Whether the orchestrator used its one extension of ``limit`` on ``s``."""
    return bool(s.extensions.get(f"{limit}:orchestrator"))


def raise_limit(ctx: Context, sessions: dict[str, Session], s: Session, limit: str, by_orchestrator: bool) -> int:
    """Add rounds.extend_by to ``s``'s ``limit``; the caller holds ctx.lock and writes the registry. The new extra."""
    s.extensions[limit] = s.extensions.get(limit, 0) + ctx.settings().get_int("rounds.extend_by")
    if by_orchestrator:
        s.extensions[f"{limit}:orchestrator"] = 1
    return s.extensions[limit]


def extend(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """One more allowance of a limit: the orchestrator's once per session and limit, the owner's any time. The
    orchestrator's second ask puts the task on the owner's decide card."""
    from runtime.queue import cards  # cards moves sessions through this module

    limit, who = text_arg(args, "limit"), actor(ctx)
    if limit not in LIMITS:
        raise Refused("args", f"extend one of {', '.join(LIMITS)}")
    with ctx.lock:
        sessions = read(ctx)
        s = find(ctx, sessions, text_arg(args, "session"))
        if s.state == RETIRED:
            raise Refused("retired", f"{s.name} is retired")
        if who != OWNER and extended_once(s, limit):
            if cards.spent(ctx, s, limit, f"{s.name} used up its {limit} again after one extension"):
                raise Refused("extend", f"{s.name}'s {limit} was extended once already; the owner decides now, on "
                                        "the card on Home")  # fmt: skip
            raise Refused("extend", f"{s.name}'s {limit} was extended once already, and its task is not running")
        extra = raise_limit(ctx, sessions, s, limit, by_orchestrator=who != OWNER)
        registry.write(ctx.places, sessions)
    if who == OWNER:
        ctx.post_event(inbox.LIMIT_EXTENDED, {"session": s.id, "name": s.name, "limit": limit, "extra": extra})
    return ok(name=s.name, limit=limit, extra=extra)
