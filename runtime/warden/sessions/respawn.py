"""Bringing a stopped session back under its own id, with a message.

Claude Code starts a copy of a background session resumed with any option, so the warden passes none: `claude respawn`
restarts the session with the options it started with, and the message follows once it runs again. Those options name
the sandbox settings file its start wrote, under the start's pending id. The warden writes that file again first, from
today's settings, so the sandbox also walls off the worktrees of agents started since.
"""

from __future__ import annotations

from pathlib import Path

from runtime.shared import store
from runtime.shared.models import Session
from runtime.warden.context import Context
from runtime.warden.sessions import backstop, launch, session_settings
from runtime.warden.sessions.launch import LaunchError


def _start_id(ctx: Context, sid: str) -> str:
    """The pending id the session started under, which names its settings file: the history's bound row holds it."""
    for row in store.read_jsonl(ctx.places.history):
        if row.get("session") == sid and isinstance(row.get("was"), str):
            return str(row["was"])
    raise LaunchError("its start is not in the history, so its sandbox settings are unknown: stop it, start a new one")


def resume(ctx: Context, s: Session, message: str) -> None:
    """Respawn a stopped session under its own id and hand it ``message``; raises LaunchError when it did not."""
    if not message.strip():
        raise LaunchError("a resume needs a message")
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
