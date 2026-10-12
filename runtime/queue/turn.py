"""The merge turn: one branch at a time onto the default branch, kept by the warden, never by a model's memory.

`factory turn next` gives the turn to the next qa-clean task whose `after` tasks have landed, in the one order, and
tells its implementer; while a task holds it no other task may. The holder and its branch are published for the
merge hook, which refuses any other branch. The warden finds the merge itself, each tick: the first-parent merge on
the default branch, since the turn was given, whose second parent is on the holder's branch. A saved check of that
merge with no failure moves the task to checked. The orchestrator keeps the merge itself and the call to revert;
the warden sends a reverted task back to its implementer, or, once that has ended, to a new one.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from runtime.queue import formats, proposals, statuses, tasks
from runtime.queue.book import Queue
from runtime.shared import order, store
from runtime.shared.models import RETIRED, WAITING, WORKING, Session, Task
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden.context import Context
from runtime.warden.sessions import lifecycle, permits, registry, revive
from runtime.warden.sessions.launch import LaunchError

log = logging.getLogger(__name__)
TURN, MERGED, CHECKED = "turn", "merged", "checked"
GIVEN = (
    "You have the merge turn. Rebase on {default}, rerun the tests you picked, then factory check; once it passes, "
    "ask the orchestrator to merge, with the tree it names."
)
REVERTED = (
    "Your work was taken back off {default}: {note}\n\nBring it back first: `git merge --ff-only {default}`, "
    "`git revert <the revert's sha>`, then the fix. A rebase alone would leave the work reverted."
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


def full_commit(ctx: Context, commit: str) -> str:
    """The full sha of a commit the checkout knows: a check is matched to its merge by the whole sha."""
    if not formats.SHA.match(commit):
        raise Refused("check", "name the commit the check ran on")
    repo = ctx.settings().get_str("repo.path")
    code, out = ctx.launcher.agent_command(["git", "-C", repo, "rev-parse", "--verify", "--quiet",
                                            f"{commit}^{{commit}}"], repo)  # fmt: skip
    if code == 1 and not out.strip():  # --verify --quiet: git found no such commit, and says nothing
        raise Refused("check", f"the checkout has no commit {commit}: name the merge the check ran on")
    if code != 0:
        log.warning("warden: git rev-parse in the agents' checkout failed (exit %d)", code)
        raise Refused("git", f"git rev-parse failed in the agents' checkout: {out.strip()[-200:]}")
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
    held = holder(ctx.places)
    if held is not None:
        raise Refused("one-turn", f"{held['task']} holds the merge turn; wait for its merge")
    queue = Queue.of(ctx)
    found, _ = queue.tasks()
    by_id = {t.id: t for t in found}
    ready = [t for t in found if t.status == "qa-clean"
             and all(a in by_id and by_id[a].status in tasks.LANDED for a in t.after)]  # fmt: skip
    if not ready:
        return ok(task=None, message="no qa-clean task is ready for its merge turn")
    task = order.ordered(ready, permits.ranking(ctx))[0]
    moved = tasks.move(ctx, task.id, TURN, "turn", statuses.ORCH, "the next merge turn")
    _tell(ctx, moved, GIVEN.format(default=ctx.settings().get_str("repo.default_branch")))
    return ok(task=moved.id, branch=moved.branch, message=f"{moved.id} has the merge turn; its implementer was told")


def _implementer(ctx: Context, task_id: str) -> Session | None:
    sessions = registry.read(ctx.places).values()
    mine = [s for s in sessions if s.task == task_id and s.role == statuses.IMPL and not s.parent]
    return next((s for s in mine if s.state != RETIRED), None)


def _tell(ctx: Context, task: Task, message: str) -> None:
    impl = _implementer(ctx, task.id)
    if impl is None:
        return
    try:
        lifecycle.deliver(ctx, impl.id, message, lifecycle.WARDEN)
    except (LaunchError, Refused) as exc:
        log.warning("warden: %s's implementer %s was not told of its move (%s)", task.id, impl.id, type(exc).__name__)


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


def send_back(ctx: Context, task_id: str, note: str) -> Task:
    """A reverted task goes back to building, its implementer told why; a benched or reviving one is told when it
    is back, and a word that did not arrive is held for the run's next start or revive.
    Once that implementer has ended (its task passed its check), the task goes back to the approved runs, or a
    designed build to the designed builds, and the scheduler starts a new implementer, told why first."""
    if not note.strip():
        raise Refused("note", "a revert says why, and names the revert's commit, in --note")
    told = REVERTED.format(default=ctx.settings().get_str("repo.default_branch"), note=note.strip())
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = queue.task(task_id)
        impl = _implementer(ctx, task_id)
        designed = task.run is not None and task.run >= 3
        to = "building" if impl is not None or designed else "approved"
        moved = tasks.transition(ctx, queue, task, to, "reverted", tasks.actor(ctx), note.strip())
        if impl is None and designed and task_id not in proposals.designed_builds(ctx.places):
            proposals.set_designed_builds(ctx.places, [*proposals.designed_builds(ctx.places), task_id])
        # The latest revert's word alone is held: an earlier one's revert is part of this one's history.
        store.write_text(lifecycle.held_path(ctx, task_id), told + "\n", mode=0o600)
        awake = impl is not None and impl.state in (WORKING, WAITING)
    if awake and impl is not None:
        revive.hand_held(ctx, impl.id, task_id, told)
    return moved
