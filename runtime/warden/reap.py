"""Reaping: removing what a retired session and its slot made, as soon as it retires, so nothing waits to be cleaned.

Only a retired session is reaped; anything else is refused. Its slot is emptied only while the slot's lease still
names it (so a slot another session claimed is never touched): the product's ``scripts/dev empty``, then every
Docker object labelled with the slot, both through the stack unit, then the lease goes. Its worktree is removed as
the agent user only when git calls it clean and no live session works in it; a dirty one is held, with git's reason,
for the next session on the same task. Its branch goes once merged. QA evidence ages out in the nightly gc.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.shared.models import RETIRED, Session
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.warden import registry
from runtime.warden.context import Context
from runtime.warden.verbs import ORCH

log = logging.getLogger("factory.warden")

# The stack unit's own verb, beside the product's scripts/dev verbs: remove every container, volume, network and
# image labelled with the request's slot.
SWEEP_SLOT = "factory-sweep-slot"


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


def _empty_slot(ctx: Context, session: Session, slot: int) -> str:
    lease = leases.read(ctx.places, slot)
    if lease is None:
        return "left alone: the slot was released before the reap"
    if lease.holder not in (session.id, session.name):
        return "left alone: another session holds the slot"
    for verb in (["empty"], [SWEEP_SLOT]):
        done = ctx.launcher.run_stack(slot, verb, session.id)
        if done.exit != 0:
            log.warning("warden: reaping session %s, slot %d %s exited %d", session.id, slot, verb[0], done.exit)
            return f"failed: {verb[0]} exited {done.exit}; see {done.log}"
    leases.release(ctx, slot)
    return "emptied"


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
        if session.slot is not None:
            outcome["slot"] = _empty_slot(ctx, session, session.slot)
        if session.worktree and session.role != ORCH:
            outcome.update(_remove_worktree(ctx, session, live))
    done = reaped(ctx.places)
    done[session_id] = {"at": ctx.clock.iso(), **outcome}
    store.write_json(_reaped_path(ctx.places), done, mode=0o600)
    return outcome


class Reaper:
    """Reaps every retired session not yet reaped, in a thread of its own so a slow stack never delays a tick."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None

    def run_pass(self, ctx: Context) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        sessions = registry.read(ctx.places)
        done = reaped(ctx.places)
        due = sorted(sid for sid, s in sessions.items() if s.state == RETIRED and sid not in done)
        if due:
            self._thread = threading.Thread(target=self._reap_all, args=(ctx, due), name="reap", daemon=True)
            self._thread.start()

    def _reap_all(self, ctx: Context, due: list[str]) -> None:
        for session_id in due:
            try:
                reap(ctx, session_id)
            except Exception as exc:
                log.exception("warden: reaping session %s failed (%s)", session_id, type(exc).__name__)
                # Recorded, so one broken reap is reported once rather than every tick; the dashboard shows it.
                done = reaped(ctx.places)
                done[session_id] = {"at": ctx.clock.iso(), "failed": type(exc).__name__}
                store.write_json(_reaped_path(ctx.places), done, mode=0o600)
