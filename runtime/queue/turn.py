"""The merge turn: one branch at a time onto the default branch, kept by the warden, never by a model's memory.

`factory turn next` gives the turn to the next qa-clean task whose `after` tasks have landed, in the one order, and
tells its implementer; while a task holds it no other task may. The holder and its branch are published for the
merge hook, which refuses any other branch. The warden finds the merge itself, each tick: the first-parent merge on
the default branch, since the turn was given, whose second parent is on the holder's branch. A saved check of that
merge with no failure moves the task to checked. The orchestrator keeps the merge itself and the call to revert.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from runtime.queue import statuses, tasks
from runtime.queue.book import Queue
from runtime.shared import order, store
from runtime.shared.models import RETIRED, Task
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden import lifecycle, permits, registry
from runtime.warden.context import Context
from runtime.warden.launch import LaunchError

log = logging.getLogger(__name__)
TURN, MERGED, CHECKED = "turn", "merged", "checked"
GIVEN = (
    "You have the merge turn. Rebase on {default}, rerun the tests you picked, then factory check; once it passes, "
    "ask the orchestrator to merge, with the tree it names."
)


def _path(places: Places) -> Path:
    return places.state / "turn.json"


def merges_path(places: Places) -> Path:
    """Every merge the warden found, by its commit: the task it landed."""
    return places.state / "merges.json"


def holder(places: Places) -> dict[str, str] | None:
    """The task holding the turn, its branch and the default branch's commit when it got it; None when free."""
    raw = store.read_json_or(_path(places), None)
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"task", "branch", "base"}:
        raise store.StoreError(f"{_path(places)}: want task, branch and base")
    return raw


def merges(places: Places) -> dict[str, str]:
    raw = store.read_json_or(merges_path(places), {})
    if not isinstance(raw, dict):
        raise store.StoreError(f"{merges_path(places)}: not an object")
    return raw


def _git(ctx: Context, *args: str) -> str:
    repo = ctx.settings().get_str("repo.path")
    code, out = ctx.launcher.agent_command(["git", "-C", repo, *args], repo)
    if code != 0:
        raise Refused("git", f"git {args[0]} failed: {out.strip()[-200:]}")
    return out.strip()


def held(ctx: Context, task: Task) -> None:
    """``task`` got the turn: record it with the default branch's commit now. The caller holds ctx.lock."""
    if holder(ctx.places) is not None:
        raise Refused("one-turn", "another task holds the merge turn; the next one goes when it merges")
    default = ctx.settings().get_str("repo.default_branch")
    record = {"task": task.id, "branch": task.branch, "base": _git(ctx, "rev-parse", f"refs/heads/{default}")}
    store.write_json(_path(ctx.places), record, mode=0o600)
    store.write_json(ctx.places.public_turn, {"task": task.id, "branch": task.branch})


def released(ctx: Context, task_id: str) -> None:
    """The task left its turn (merged, sent back, stopped)."""
    found = holder(ctx.places)
    if found is not None and found["task"] == task_id:
        _path(ctx.places).unlink()
        ctx.places.public_turn.unlink(missing_ok=True)


def next_turn(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """`factory turn next`: the turn goes to the first qa-clean task, in the one order, whose after tasks landed."""
    if holder(ctx.places) is not None:
        raise Refused("one-turn", f"{holder(ctx.places)['task']} holds the merge turn; wait for its merge")  # type: ignore[index]
    queue = Queue.of(ctx)
    found, _ = queue.tasks()
    by_id = {t.id: t for t in found}
    ready = [t for t in found if t.status == "qa-clean"
             and all(a in by_id and by_id[a].status in tasks.LANDED for a in t.after)]  # fmt: skip
    if not ready:
        return ok(task=None, message="no qa-clean task is ready for its merge turn")
    task = order.ordered(ready, permits.approved_order(ctx))[0]
    moved = tasks.move(ctx, task.id, TURN, "turn", statuses.ORCH, "the next merge turn")
    _tell(ctx, moved, GIVEN.format(default=ctx.settings().get_str("repo.default_branch")))
    return ok(task=moved.id, branch=moved.branch, message=f"{moved.id} has the merge turn; its implementer was told")


def _tell(ctx: Context, task: Task, message: str) -> None:
    sessions = registry.read(ctx.places).values()
    mine = [s for s in sessions if s.task == task.id and s.role == statuses.IMPL and not s.parent]
    impl = next((s for s in mine if s.state != RETIRED), None)
    if impl is None:
        return
    try:
        lifecycle.deliver(ctx, impl.id, message, lifecycle.WARDEN)
    # telemetry: surfaced — the turn stands; the implementer reads it from its task's status when it next looks
    except (LaunchError, Refused) as exc:
        log.warning("warden: %s's implementer %s was not told of its turn (%s)", task.id, impl.id, type(exc).__name__)


def find_merge(ctx: Context) -> None:
    """The tick's step: the holder's merge on the default branch, recorded as soon as it is there."""
    found = holder(ctx.places)
    if found is None:
        return
    default = ctx.settings().get_str("repo.default_branch")
    on_branch = set(_git(ctx, "rev-list", f"{found['base']}..refs/heads/{found['branch']}").split())
    for line in _git(
        ctx, "rev-list", "--first-parent", "--merges", "--parents", f"{found['base']}..{default}"
    ).splitlines():
        commit, *parents = line.split()
        if len(parents) >= 2 and parents[1] in on_branch:
            known = merges(ctx.places)
            store.write_json(merges_path(ctx.places), {**known, commit: found["task"]}, mode=0o600)
            tasks.move(ctx, found["task"], MERGED, "merged", statuses.WARDEN, f"merged as {commit}", commit=commit)
            log.info("warden: %s merged as %s", found["task"], commit)
            return


def check_saved(ctx: Context, commit: str, passed: bool) -> str:
    """A check of the default branch was saved: the task that merge landed is checked when nothing failed. The task
    moved, or ""."""
    task_id = merges(ctx.places).get(commit, "")
    if not task_id or not passed:
        return ""
    if Queue.of(ctx).task(task_id).status != MERGED:
        return ""
    tasks.move(ctx, task_id, CHECKED, "checked", statuses.WARDEN, f"the check of {commit[:12]} passed")
    return task_id
