"""The merge turn: one branch at a time onto the default branch, kept by the warden, never by a model's memory.

`factory turn next` gives the turn to the next qa-clean task whose `after` tasks have landed and whose implementer
is awake, in the one order, and tells its implementer; while a task holds it no other task may. The holder and its
branch are published for the merge hook, which refuses any other branch. The warden finds the merge itself, each
tick: the first-parent merge on the default branch, since the turn was given, whose second parent is on the holder's
branch; a check clears it with the merges bundled with it (bundle.py). Until then, a holder whose implementer was
benched, outside the owner's pause, or ended goes back to qa-clean, so the next task merges first. The warden
benches the merged task's implementer, which holds no place while the check runs. The orchestrator keeps the merge
itself and the call to revert or block; the warden sends a reverted task, or a blocked one at the owner's Fixed, back
to its implementer, or, once that has ended, to a new one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.queue import cards, formats, proposals, statuses, tasks
from runtime.queue.book import Queue
from runtime.shared import order, store
from runtime.shared.models import DEPRIORITIZED, PAUSED, QA_CLEAN, RETIRED, WAITING, WORKING, Session, Task
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden.context import Context
from runtime.warden.owner import reflections
from runtime.warden.sessions import lifecycle, permits, registry, revive
from runtime.warden.sessions.launch import LaunchError

log = logging.getLogger(__name__)
TURN, MERGED, CHECKED, TURN_BACK = "turn", "merged", "checked", "turn-back"
GAVE_BACK = "its implementer was benched or ended before its merge: the next task merges first"
GIVEN = (
    "You have the merge turn. Rebase on {default}, rerun the tests you picked, then factory check; once it passes, "
    "ask the orchestrator to merge, with the tree it names."
)
_BRING_BACK = (
    "Bring it back first: `git merge --ff-only {default}`, `git revert <the revert's sha>`, then the fix. A rebase "
    "alone would leave the work reverted."
)
REVERTED = "Your work was taken back off {default}: {note}\n\n" + _BRING_BACK
BLOCKED = "Your task was blocked: {note}\n\nThe owner fixed what blocked it: carry on."
BLOCKED_OFF = BLOCKED + " It was taken back off {default}. " + _BRING_BACK
HELD = "Your task was put on hold: {note}\n\n{after}, which it waited for, passed its check: carry on."
RELEASED = "Your task was put on hold, and the orchestrator let it go on without what it waited for: {note}"
HELD_GONE = (
    "Your task was put on hold: {note}\n\n{after}, which it waited for, was {how} before its check passed: find "
    "another way, or report what still stops it."
)
_OFF = " It was taken back off {default}. " + _BRING_BACK
PASSED = tasks.LANDED - {MERGED, "closed"}
GONE = frozenset({"closed", "parked"})  # a task here makes nothing a held task waits for


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


def git(ctx: Context, *args: str) -> str:
    """What git prints in the agents' checkout; a failure is refused with git's own last words."""
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
    record = {"task": task.id, "branch": task.branch, "base": git(ctx, "rev-parse", f"refs/heads/{default}")}
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
    return give(ctx, lambda task: True, "no qa-clean task is ready for its merge turn")


def give(ctx: Context, fits: Callable[[Task], bool], none: str) -> dict[str, Any]:
    """The turn to the first ready task that ``fits``; ``none`` says why none got it."""
    held = holder(ctx.places)
    if held is not None:
        raise Refused("one-turn", f"{held['task']} holds the merge turn; wait for its merge")
    queue = Queue.of(ctx)
    found, _ = queue.tasks()
    by_id = {t.id: t for t in found}
    ready = [t for t in found if t.status == QA_CLEAN and fits(t) and _awake(ctx, t.id)
             and all(a in by_id and by_id[a].status in tasks.LANDED for a in t.after)]  # fmt: skip
    if not ready:
        return ok(task=None, message=none)
    task = order.ordered(ready, permits.ranking(ctx))[0]
    moved = tasks.move(ctx, task.id, TURN, "turn", statuses.ORCH, "the next merge turn")
    _tell(ctx, moved, GIVEN.format(default=ctx.settings().get_str("repo.default_branch")))
    return ok(task=moved.id, branch=moved.branch, message=f"{moved.id} has the merge turn; its implementer was told")


def _implementer(ctx: Context, task_id: str) -> Session | None:
    sessions = registry.read(ctx.places).values()
    mine = [s for s in sessions if s.task == task_id and s.role == statuses.IMPL and not s.parent]
    return next((s for s in mine if s.state != RETIRED), None)


def _awake(ctx: Context, task_id: str) -> bool:
    impl = _implementer(ctx, task_id)
    return impl is not None and impl.state in (WORKING, WAITING)


def _tell(ctx: Context, task: Task, message: str) -> None:
    impl = _implementer(ctx, task.id)
    if impl is None:
        return
    try:
        lifecycle.deliver(ctx, impl.id, message, lifecycle.WARDEN)
    except (LaunchError, Refused) as exc:
        log.warning("warden: %s's implementer %s was not told of its move (%s)", task.id, impl.id, type(exc).__name__)


def find_merge(ctx: Context) -> None:
    """The tick's step: the holder's merge on the default branch, recorded as soon as it is there; with none, a holder
    whose implementer was benched or ended gives the turn back."""
    found = holder(ctx.places)
    if found is None:
        return
    default = ctx.settings().get_str("repo.default_branch")
    on_branch = set(git(ctx, "rev-list", f"{found['base']}..refs/heads/{found['branch']}").split())
    for line in git(
        ctx, "rev-list", "--first-parent", "--merges", "--parents", f"{found['base']}..{default}"
    ).splitlines():
        commit, *parents = line.split()
        if len(parents) >= 2 and parents[1] in on_branch:
            known = merges(ctx.places)
            store.write_json(merges_path(ctx.places), {**known, commit: found["task"]}, mode=0o600)
            tasks.move(ctx, found["task"], MERGED, "merged", statuses.WARDEN, f"merged as {commit}", commit=commit)
            _bench(ctx, found["task"], f"merged as {commit[:12]}: benched until its check")
            log.info("warden: %s merged as %s", found["task"], commit)
            return
    impl = _implementer(ctx, found["task"])
    if impl is None or (impl.state == DEPRIORITIZED and impl.benched_for != PAUSED):
        tasks.move(ctx, found["task"], QA_CLEAN, TURN_BACK, statuses.WARDEN, GAVE_BACK)


def _bench(ctx: Context, task_id: str, why: str) -> None:
    """The task's implementer gives up its place: benched, it comes back only when the task is sent back to it."""
    with ctx.lock:
        sessions = registry.read(ctx.places)
        impl = _implementer(ctx, task_id)
        if impl is None or impl.state not in (WORKING, WAITING):
            return
        lifecycle.begin_bench(ctx, sessions[impl.id], lifecycle.WARDEN, why, task_id)
        registry.write(ctx.places, sessions)


def take_off_course(ctx: Context, task_id: str, to: str, event: str, note: str, after: str) -> Task:
    """The orchestrator's moves off a task's course: a revert, a block, a hold, and the release of its own hold."""
    if event in (tasks.BLOCKED, tasks.ON_HOLD) and to != event:
        raise Refused("task-status", f"--event {event} moves {task_id} to {event}, nowhere else")
    if event == tasks.BLOCKED:
        return block(ctx, task_id, note)
    if event == tasks.ON_HOLD:
        return hold(ctx, task_id, note, after)
    if event == tasks.RELEASED:
        return release(ctx, task_id, note)
    if to != "building":
        raise Refused("task-status", f"a revert sends {task_id} back to building; the warden finds who builds it")
    return send_back(ctx, task_id, note)


def send_back(ctx: Context, task_id: str, note: str) -> Task:
    """A reverted task goes back to building, its implementer told why; a benched or reviving one is told when it
    is back, and a word that did not arrive is held for the run's next start or revive.
    Once that implementer has ended (its task passed its check), the task goes back to the approved runs, or a
    designed build to the designed builds, and the scheduler starts a new implementer, told why first."""
    if not note.strip():
        raise Refused("note", "a revert says why, and names the revert's commit, in --note")
    told = REVERTED.format(default=ctx.settings().get_str("repo.default_branch"), note=note.strip())
    return _back(ctx, task_id, "reverted", tasks.actor(ctx), note.strip(), told)


def block(ctx: Context, task_id: str, note: str) -> Task:
    """The environment stopped the task: it waits benched, off the default branch, for the owner's Fixed."""
    if not note.strip():
        raise Refused("note", "a block names what stopped the task, and the revert's commit once merged, in --note")
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = queue.task(task_id)
        moved = tasks.transition(ctx, queue, task, "blocked", "blocked", tasks.actor(ctx), note.strip())
        cards.blocked(ctx, moved, note)
    _bench(ctx, task_id, f"blocked: {formats.one_line(note)}")
    reflections.incident(ctx, f"{task_id} is blocked until the owner fixes what stopped it: {note}", f"task {task_id}")
    return moved


def hold(ctx: Context, task_id: str, note: str, after: str) -> Task:
    """Another task makes what stopped this one (a seed, a scenario): it waits benched, off the default branch, until
    that task's check passes, and the warden brings it back (``release_held``). Nothing waits on the owner."""
    if not note.strip():
        raise Refused("note", "a hold names what stopped the task, and the revert's commit once merged, in --note")
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = queue.task(task_id)
        if not after or after == task_id:
            raise Refused("task-status", f"a hold names, in --after, the task {task_id} waits for")
        by_id = {t.id: t for t in queue.tasks()[0]}
        waited = by_id.get(after)
        if waited is None:
            raise Refused("task-status", f"there is no task {after} for {task_id} to wait for")
        if waited.status in PASSED:
            raise Refused("task-status", f"{after} passed its check already: send {task_id} back instead")
        if waited.status in GONE:
            raise Refused("task-status", f"{after} is {waited.status}, so nothing makes what {task_id} lacks")
        if task_id in _upstream(by_id, after):
            raise Refused("task-status", f"{after} waits, through its after tasks, for {task_id}: neither would move")
        waits = task.after if after in task.after else [*task.after, after]
        on_hold = tasks.ON_HOLD
        moved = tasks.transition(ctx, queue, task, on_hold, on_hold, tasks.actor(ctx), note.strip(), after=waits)
    _bench(ctx, task_id, f"on hold until {after} passes its check: {formats.one_line(note)}")
    return moved


def _upstream(by_id: dict[str, Task], start: str) -> set[str]:
    seen: set[str] = set()
    todo = [start]
    while todo:
        found = todo.pop()
        if found in by_id and found not in seen:
            seen.add(found)
            todo += by_id[found].after
    return seen


def release_held(ctx: Context) -> None:
    """The warden's pass: a held task goes back, told why, once all it waits for passed its check, or one is gone."""
    queue = Queue.of(ctx)
    found, _ = queue.tasks()
    by_id = {t.id: t for t in found}
    for task in found:
        waits = [by_id.get(a) for a in task.after]
        gone = [w for w in waits if w is not None and w.status in GONE]
        if task.status != tasks.ON_HOLD or not (gone or all(w is not None and w.status in PASSED for w in waits)):
            continue
        held = next((e for e in reversed(queue.read_log(task.id)) if e["event"] == tasks.ON_HOLD), None)
        note, off = ("", False) if held is None else (held["text"], held.get("from") == MERGED)
        waited = ", ".join(w.id for w in gone) or ", ".join(task.after)
        how = " or ".join(sorted({w.status for w in gone}))
        told = (HELD_GONE if gone else HELD).format(note=note, after=waited, how=how)
        told += _OFF.format(default=ctx.settings().get_str("repo.default_branch")) if off else ""
        _back(ctx, task.id, tasks.RELEASED, statuses.WARDEN, f"{waited} {how or 'passed its check'}", told)


def release(ctx: Context, task_id: str, note: str) -> Task:
    """A hold the orchestrator lets go: the task goes back as a revert does, and waits for, and merges after, none."""
    if not note.strip():
        raise Refused("note", "a release says why the task need not wait, in --note")
    told = RELEASED.format(note=note.strip())
    return _back(ctx, task_id, tasks.RELEASED, statuses.ORCH, note.strip(), told, after=[])


def unblock(ctx: Context, task_id: str) -> Task:
    """The owner fixed what blocked the task: it goes back as a revert does, told what stopped it."""
    block = next((e for e in reversed(Queue.of(ctx).read_log(task_id)) if e["event"] == "blocked"), None)
    if block is None:
        raise Refused("task-status", f"{task_id} was never blocked")
    word = BLOCKED_OFF if block.get("from") == MERGED else BLOCKED
    told = word.format(default=ctx.settings().get_str("repo.default_branch"), note=block["text"])
    return _back(ctx, task_id, "unblocked", statuses.OWNER, "the owner fixed what blocked it", told)


def _back(ctx: Context, task_id: str, event: str, by: str, note: str, told: str, **fields: Any) -> Task:
    """Back to its implementer, or to a new one once that has ended, told ``told`` first."""
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = queue.task(task_id)
        impl = _implementer(ctx, task_id)
        designed = task.run is not None and task.run >= 3
        to = "building" if impl is not None or designed else "approved"
        moved = tasks.transition(ctx, queue, task, to, event, by, note, **fields)
        if impl is None and designed and task_id not in proposals.designed_builds(ctx.places):
            proposals.set_designed_builds(ctx.places, [*proposals.designed_builds(ctx.places), task_id])
        # The latest revert's word alone is held: an earlier one's revert is part of this one's history.
        store.write_text(lifecycle.held_path(ctx, task_id), told + "\n", mode=0o600)
        awake = impl is not None and impl.state in (WORKING, WAITING)
    if awake and impl is not None:
        revive.hand_held(ctx, impl.id, task_id, told)
    return moved
