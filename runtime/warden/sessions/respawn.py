"""Bringing a stopped session back, with a message.

Claude Code starts a copy of a background session resumed with any option, so the warden passes none: `claude respawn`
restarts the session with the options it started with, and the message follows once it runs again. Those options name
the sandbox settings file its start wrote, under the start's pending id. The warden writes that file again first, from
today's settings, so the sandbox also walls off the worktrees of agents started since.

Its agent definitions are in the options themselves, so a respawn keeps the ones it started with. When those differ
from today's, the warden asks for the copy instead: `claude --bg --resume <id>` with today's options, from the
session's own checkout, carries its whole conversation into a new id. The session's row waits under a pending id,
reviving, until the backstop binds it to the copy's, as a start's is. Then all its old id held follows (its QA records,
its subagents, its doing line, its skills note), and the history leads the old id to the new. An owner's stop
meanwhile stays. A copy that does not run within the start grace, or any failure, puts the row back under its old id
as it is then; a copy running after that is a stray the backstop stops. The stopped original is never removed:
removing it deletes the worktree the copy runs in.
"""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.models import PENDING, RETIRED, REVIVING, Session
from runtime.warden.context import Context
from runtime.warden.sessions import backstop, launch, older_skills, registry, session_settings
from runtime.warden.sessions.launch import LaunchError

RENEWED = "came back as a copy, with today's agent definitions"
PUT_BACK = "its copy did not come back, so it stays under its own id"


def _start_id(ctx: Context, sid: str) -> str:
    """The pending id the session started under, which names its settings file: the history's bound row holds it."""
    for row in store.read_jsonl(ctx.places.history):
        if row.get("session") == sid and isinstance(row.get("was"), str):
            return str(row["was"])
    raise LaunchError("its start is not in the history, so its sandbox settings are unknown: stop it, start a new one")


def resume(ctx: Context, s: Session, message: str) -> str:
    """Bring stopped session ``s`` back with ``message``; the id it then runs under. Raises LaunchError if not."""
    if not message.strip():
        raise LaunchError("a resume needs a message")
    agents = launch.definitions(ctx, s.role, s.model, s.effort)
    if session_settings.fingerprint(agents) != s.definitions:
        return _renew(ctx, s, message, agents)
    settings = ctx.settings()
    session_settings.write(settings, ctx.places, _start_id(ctx, s.id), s.role, Path(s.worktree), launch.agent_home())
    code, output = ctx.launcher.agent_command(["claude", "respawn", s.id[:8]], settings.get_str("repo.path"))
    if code != 0:
        raise LaunchError(f"claude respawn failed: {output.strip()[-300:]}")
    grace = settings.get_int("watch.start_grace_seconds")
    deadline = ctx.clock.now() + grace
    # The message is `claude --resume` in a terminal, which would start the session there if it were not running.
    while s.id not in backstop.processes():
        if ctx.clock.now() >= deadline:
            raise LaunchError(f"it did not run again within {grace} seconds of its respawn")
        ctx.clock.sleep(settings.get_int("watch.respawn_poll_seconds"))
    ctx.launcher.message_session(s.id, message)
    return s.id


def _renew(ctx: Context, s: Session, message: str, agents: dict[str, Any]) -> str:
    request_id = f"{PENDING}{uuid.uuid4().hex}"
    carried = launch.options(ctx, request_id, s, agents)
    repo = Path(ctx.settings().get_str("repo.path"))
    with ctx.lock:
        sessions = registry.read(ctx.places)
        before = dataclasses.replace(sessions.pop(s.id))
        # Reviving while the copy starts: the crash watch leaves it alone, and the backstop lets its process run.
        row = dataclasses.replace(before, id=request_id, state=REVIVING, renewed=ctx.clock.iso(), copied_from=s.id,
                                  definitions=session_settings.fingerprint(agents))  # fmt: skip
        sessions[request_id] = row
        registry.write(ctx.places, sessions)
    worktree = "" if Path(s.worktree) == repo else Path(s.worktree).name
    request = {"name": s.name, "cwd": str(repo), "worktree": worktree, "prompt": message, "resume": s.id, **carried}
    try:
        ctx.launcher.start_agent(request_id, request)
        return _bound(ctx, before, row)
    except Exception:
        _put_back(ctx, before, row)
        raise


def _holder(sessions: dict[str, Session], row: Session) -> str | None:
    return next((sid for sid, s in sessions.items() if s.renewed == row.renewed and s.name == row.name), None)


def _bound(ctx: Context, before: Session, row: Session) -> str:
    """The copy's id once it runs; LaunchError when the owner stopped it first, or past the start grace."""
    grace = ctx.settings().get_int("watch.start_grace_seconds")
    deadline = ctx.clock.now() + grace
    while True:
        with ctx.lock:
            sessions = registry.read(ctx.places)
            if row.id in sessions:
                backstop.bind_running(ctx.places, ctx.clock, sessions, backstop.processes())
            sid = _holder(sessions, row)
            if sid is not None and not sid.startswith(PENDING):
                if sessions[sid].state == REVIVING:
                    sessions[sid].state = before.state
                _follow(ctx, sessions, before.id, sid, RENEWED)
                registry.write(ctx.places, sessions)
                return sid
            if sid is None or sessions[sid].state == RETIRED:
                raise LaunchError("it was stopped while its copy with today's agent definitions started")
            if ctx.clock.now() >= deadline:
                raise LaunchError(f"its copy with today's agent definitions did not run within {grace} seconds")
        ctx.clock.sleep(ctx.settings().get_int("watch.respawn_poll_seconds"))


def _follow(ctx: Context, sessions: dict[str, Session], was: str, sid: str, why: str) -> None:
    """All id ``was`` held moves to ``sid``; the caller holds the warden's lock, and writes ``sessions`` after."""
    for old, new in zip(ctx.places.session_files(was), ctx.places.session_files(sid), strict=True):
        if old.exists():
            old.replace(new)
    for child in registry.children(sessions, was):
        child.parent = sid
    older_skills.renamed(ctx.places, was, sid)
    line = {"at": ctx.clock.iso(), "session": sid, "was": was, "name": sessions[sid].name, "by": "warden", "why": why}
    store.append_jsonl(ctx.places.history, line)


def _put_back(ctx: Context, before: Session, row: Session) -> None:
    """The session goes back under its own id and first definitions, as it is now."""
    with ctx.lock:
        sessions = registry.read(ctx.places)
        sid = _holder(sessions, row)
        now = sessions.pop(sid) if sid is not None else before
        state = before.state if now.state == REVIVING else now.state
        sessions[before.id] = dataclasses.replace(now, id=before.id, state=state, renewed=before.renewed,
                                                  copied_from=before.copied_from, definitions=before.definitions,
                                                  kinds=before.kinds)  # fmt: skip
        if sid is not None:
            _follow(ctx, sessions, sid, before.id, PUT_BACK)
        registry.write(ctx.places, sessions)
