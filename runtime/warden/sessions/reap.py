"""Reaping: removing what a retired session and its slot made, as soon as it retires, so nothing waits to be cleaned.

Only a retired session is reaped; anything else is refused. The slot whose lease names it is emptied (so a slot another
session claimed is never touched): the product's ``scripts/dev empty``, then every Docker object labelled with the slot,
both through the stack unit, then the lease goes. A reap that failed is tried again after watch.reap_retry_minutes. Its
worktree is removed as the agent user only when git calls it clean and no live session works in it; a dirty one is held,
with git's reason, for the next session on the same task. Its branch goes once merged. A tester's desktop goes down once
it has handed back. QA evidence ages out in the clean-up (gc.py).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from runtime.guard.facts import read_desktops
from runtime.queue import formats
from runtime.shared import store
from runtime.shared.models import RETIRED, Session
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.warden import public
from runtime.warden.context import Context
from runtime.warden.sessions import launch, registry
from runtime.warden.verbs import ORCH

log = logging.getLogger("factory.warden")


def _reaped_path(places: Places) -> Path:
    return places.state / "reaped.json"


def _held_path(places: Places) -> Path:
    return places.state / "held-worktrees.json"


def _read(path: Path) -> dict[str, Any]:
    raw = store.read_json_or(path, {})
    if not isinstance(raw, dict):
        raise store.StoreError(f"{path}: not an object")
    return raw


def reaped(places: Places) -> dict[str, Any]:
    """What each reaped session's reap did, by session id."""
    return _read(_reaped_path(places))


def held_for(places: Places, task: str) -> dict[str, Any] | None:
    """A dirty worktree kept for ``task``'s next session: worktree, branch, session, reason and when."""
    found = _read(_held_path(places)).get(task)
    return found if isinstance(found, dict) else None


def release_held(places: Places, task: str) -> None:
    """The next session for the task took the held worktree over."""
    held = _read(_held_path(places))
    if held.pop(task, None) is not None:
        store.write_json(_held_path(places), held, mode=0o600)


def _empty_slot(ctx: Context, session: Session, live: list[Session]) -> dict[str, str]:
    """The slot whose lease names the session, emptied; nothing when it holds none (released, or never claimed).
    A lease names a session by its name, which the next run of the same task takes: while that one lives, the slot
    is its own."""
    lease = leases.held_by(ctx.places, ctx.settings(), session.name)
    if lease is None:
        return {}
    if any(other.name == session.name for other in live):
        return {"slot": "left alone: the task's next session holds it"}
    failed = leases.empty(ctx, lease.slot, session.id)
    if failed:
        log.warning("warden: reaping session %s, slot %d: %s", session.id, lease.slot, failed)
        return {"slot": f"failed: {failed}"}
    return {"slot": "emptied"}


def _remove_worktree(ctx: Context, session: Session, live: list[Session]) -> dict[str, str]:
    repo = ctx.settings().get_str("repo.path")
    worktree = session.worktree
    if any(other.worktree == worktree for other in live):
        return {"worktree": "left alone: a live session works in it"}
    if not Path(worktree).is_dir():
        return {"worktree": "already gone"}
    code, out = ctx.launcher.agent_command(["git", "rev-parse", "--abbrev-ref", "HEAD"], worktree)
    branch = out.strip() if code == 0 else ""
    code, out = ctx.launcher.agent_command(["git", "worktree", "remove", worktree], repo)
    if code != 0:
        reason = (out.strip().splitlines() or ["git refused to remove it"])[-1][:300]
        held = _read(_held_path(ctx.places))
        held[session.task] = {"worktree": worktree, "branch": branch, "session": session.id, "reason": reason,
                              "at": ctx.clock.iso()}  # fmt: skip
        store.write_json(_held_path(ctx.places), held, mode=0o600)
        return {"worktree": f"held: {reason}", "branch": branch}
    if not branch or branch in ("HEAD", ctx.settings().get_str("repo.default_branch")):
        return {"worktree": "removed"}
    # -d (never -D) deletes only a branch already merged into the main checkout's branch.
    code, _ = ctx.launcher.agent_command(["git", "branch", "-d", branch], repo)
    return {"worktree": "removed", "branch": f"{branch} {'deleted' if code == 0 else 'kept: not merged'}"}


def reap(ctx: Context, session_id: str) -> dict[str, str]:
    """Reap one retired session; returns what was done to its slot, worktree and branch."""
    with ctx.lock:
        sessions = registry.read(ctx.places)
    session = sessions.get(session_id)
    if session is None:
        raise Refused("reap", f"the registry has no session {session_id}")
    if session.state != RETIRED:
        raise Refused("reap", f"{session.name} is {session.state}; only a retired session is reaped")
    live = [s for s in sessions.values() if s.state != RETIRED]
    outcome: dict[str, str] = {}
    # A subagent shares its parent's worktree and slot; the orchestrator's worktree is the main checkout.
    if not session.parent:
        outcome.update(_empty_slot(ctx, session, live))
        if session.worktree and session.role != ORCH:
            outcome.update(_remove_worktree(ctx, session, live))
    done = reaped(ctx.places)
    done[session_id] = {"at": ctx.clock.iso(), **outcome}
    store.write_json(_reaped_path(ctx.places), done, mode=0o600)
    return outcome


def _left_desktops(places: Places, sessions: dict[str, Session]) -> dict[str, str]:
    return {slot: agent for slot, agent in read_desktops(places).items()
            if agent not in sessions or sessions[agent].state == RETIRED}  # fmt: skip


def close_desktops(ctx: Context) -> None:
    """Take down each desktop whose tester handed back, from its parent's worktree. Its lease holds till then, so no
    `desktop up` starts it half-way, and goes even when the desktop would not go down, which reports."""
    for slot, agent in sorted(_left_desktops(ctx.places, registry.read(ctx.places)).items()):
        try:
            exit_code = ctx.launcher.run_stack(int(slot), ["desktop", "down"], agent).exit
        except launch.LaunchError as exc:
            log.warning("warden: slot %s's desktop down did not run after %s handed back: %s", slot, agent, exc)
        else:
            if exit_code != 0:
                log.warning("warden: slot %s's desktop did not go down after %s handed back (exit %d)", slot, agent,
                            exit_code)  # fmt: skip
        with ctx.lock:
            held = read_desktops(ctx.places)
            if held.get(slot) == agent:
                del held[slot]
                store.write_json(public.desktops_path(ctx.places), held)


def _retry_due(ctx: Context, record: dict[str, str], retry: int) -> bool:
    """A reap that left something undone (its slot, or the whole reap) runs again once ``retry`` seconds passed."""
    failed = "failed" in record or str(record.get("slot", "")).startswith("failed")
    return failed and ctx.clock.now() - formats.epoch(record["at"]) >= retry


class Reaper:
    """Reaps every retired session not yet reaped, and takes their desktops down, in a thread of its own."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None

    def run_pass(self, ctx: Context) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        sessions = registry.read(ctx.places)
        done = reaped(ctx.places)
        retry = ctx.settings().get_int("watch.reap_retry_minutes") * 60
        due = sorted(sid for sid, s in sessions.items()
                     if s.state == RETIRED and (sid not in done or _retry_due(ctx, done[sid], retry)))  # fmt: skip
        if due or _left_desktops(ctx.places, sessions):
            self._thread = threading.Thread(target=self._reap_all, args=(ctx, due), name="reap", daemon=True)
            self._thread.start()

    def _reap_all(self, ctx: Context, due: list[str]) -> None:
        try:
            close_desktops(ctx)
        except Exception as exc:
            log.exception("warden: taking the left desktops down failed (%s)", type(exc).__name__)
        for session_id in due:
            try:
                reap(ctx, session_id)
            except Exception as exc:
                log.exception("warden: reaping session %s failed (%s)", session_id, type(exc).__name__)
                # Recorded, so one broken reap is reported once rather than every tick; the dashboard shows it.
                done = reaped(ctx.places)
                done[session_id] = {"at": ctx.clock.iso(), "failed": type(exc).__name__}
                store.write_json(_reaped_path(ctx.places), done, mode=0o600)
