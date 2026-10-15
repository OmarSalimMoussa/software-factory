"""`factory check`: the warden runs the standard checks on the caller's own checkout and keeps every pass itself.

The checks run in the check-run unit (runtime/system/runners/check_run.py), walled into the checkout. A pass is keyed
by the git tree the run read, so it never covers other content: a merge whose tree equals a checked branch's tree
holds nothing the checks did not see, and a revert back to a checked state is already checked. The warden records a
pass only for a whole run that exited 0 on a committed tree that was the same before and after it. That exit status
is systemd's, written as root, so nothing the check runs can forge a pass. The merge hook reads the passed trees from
public/, which only the warden writes; the push asks ``passed``.

An implementer runs the whole set only on its merge turn, at most rounds.check_runs times a turn plus what its
session was extended by; a run of named checks (--only) costs nothing. On the default branch a check covers what
changed since the newest commit there whose tree passed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from checks.runner import catalog
from runtime.queue.book import Queue
from runtime.shared import store, upgrades
from runtime.shared.models import RETIRED, Session
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.shared.settings import places_for_test_runs
from runtime.warden.context import Context
from runtime.warden.sessions import registry

IMPL = "implementer"
TURN = "turn"
_GIT = ["git", "-c", "core.fsmonitor=false"]  # an fsmonitor the checkout's config names never runs


def _runs_path(places: Places) -> Path:
    return places.state / "check-runs.json"


def _passes(places: Places) -> list[dict[str, str]]:
    raw = store.read_json_or(places.check_passes, [])
    if not isinstance(raw, list) or not all(isinstance(p, dict) and isinstance(p.get("tree"), str) for p in raw):
        raise store.StoreError(f"{places.check_passes}: not a list of passes")
    return raw


def passed(places: Places, tree: str) -> bool:
    return any(p["tree"] == tree for p in _passes(places))


def _runs(places: Places) -> dict[str, int]:
    raw = store.read_json_or(_runs_path(places), {})
    if not isinstance(raw, dict) or not all(isinstance(v, int) for v in raw.values()):
        raise store.StoreError(f"{_runs_path(places)}: not a count per task")
    return raw


def new_turn(ctx: Context, task_id: str) -> None:
    """A task got the merge turn: its implementer's whole runs count from none again. The caller holds ctx.lock."""
    runs = _runs(ctx.places)
    if runs.pop(task_id, None) is not None:
        store.write_json(_runs_path(ctx.places), runs)


def _record(ctx: Context, tree: str, commit: str, branch: str, task: str) -> None:
    with ctx.lock:
        found = _passes(ctx.places)
        if not any(p["tree"] == tree for p in found):
            found.append({"tree": tree, "commit": commit, "branch": branch, "task": task, "at": ctx.clock.iso()})
            store.write_json(ctx.places.check_passes, found)
        store.write_json(ctx.places.public_check_passes, {"trees": sorted({p["tree"] for p in found})})


def _git(ctx: Context, checkout: str, *args: str) -> str:
    code, out = ctx.launcher.agent_command([*_GIT, *args], checkout)
    if code != 0:
        raise Refused("check", f"git {args[0]} failed in {checkout}: {out.strip()[-300:]}")
    return out


def _state(ctx: Context, checkout: str) -> tuple[str, str, str, bool]:
    """The checkout's commit, tree and branch ("HEAD" when detached), and whether it has uncommitted changes."""
    dirty = bool(_git(ctx, checkout, "status", "--porcelain", "--untracked-files=no").strip())
    commit, tree, branch = _git(ctx, checkout, "rev-parse", "HEAD", "HEAD^{tree}", "--abbrev-ref", "HEAD").split()
    return commit, tree, branch, dirty


def _session(ctx: Context) -> Session:
    session = registry.read(ctx.places).get(ctx.caller.session)
    if session is None or session.state == RETIRED or not session.worktree:
        raise Refused("check", "only a live factory session checks its own checkout")
    return session


def _only(args: dict[str, Any]) -> list[str] | None:
    only = args.get("only")
    if only is None:
        return None
    if not isinstance(only, list) or not only or not all(isinstance(c, str) for c in only):
        raise Refused("args", "--only names checks, comma-separated")
    unknown = [c for c in only if c not in catalog.IDS]
    if unknown:
        raise Refused("args", f"no check named {', '.join(unknown)}; the checks: {', '.join(catalog.IDS)}")
    return only


def _count_run(ctx: Context, session: Session, spend: bool) -> int:
    """An implementer's whole run on its merge turn, counted if ``spend``; the runs left. Refused off or past it."""
    with ctx.lock:
        task = Queue.of(ctx).task(session.task)
        if task.status != TURN:
            raise Refused("check-runs", "the whole set runs on your merge turn; while you build, run the checks "
                                        "your change needs: factory check --only tests")  # fmt: skip
        runs = _runs(ctx.places)
        done = runs.get(task.id, 0)
        limit = ctx.settings().get_int("rounds.check_runs") + session.extensions.get("check_runs", 0)
        if done >= limit:
            from runtime.queue import cards  # cards reaches the queue's tasks, which reach this module

            if cards.spent(ctx, session, "check_runs", f"{session.name} used up its check runs again"):
                raise Refused("check-runs", f"{done} check runs already this merge turn (limit {limit}); the owner "
                                            "decides now: stop and wait")  # fmt: skip
            raise Refused("check-runs", f"{done} check runs already this merge turn (limit {limit}); tell the "
                                        "orchestrator what fails")  # fmt: skip
        if spend:
            runs[task.id] = done + 1
            store.write_json(_runs_path(ctx.places), runs)
    return limit - done - 1


def _since(ctx: Context, checkout: str) -> list[str]:
    """On the default branch: from the newest first-parent commit whose tree passed, or everything when none did."""
    trees = {p["tree"] for p in _passes(ctx.places)}
    for line in _git(ctx, checkout, "log", "--first-parent", "--format=%H %T", "HEAD").splitlines():
        commit, tree = line.split()
        if tree in trees:
            return ["--since", commit]
    return ["--all"]


@contextmanager
def _place(ctx: Context, session: Session, only: list[str] | None, counted: bool) -> Iterator[int | None]:
    """A place held while a check runs tests, asked after the merge turn; none free refuses it. Yields the runs left."""
    if only is not None and "tests" not in only:
        yield None
        return
    if counted:
        _count_run(ctx, session, spend=False)
    ceiling = upgrades.installed_settings(ctx.places).get_int("budget.test_memory_ceiling_mib")
    places = places_for_test_runs(ctx.settings().__getitem__, ceiling)
    if places < 1:
        raise Refused(
            "check-places",
            "the factory's memory holds no check's tests: budget.slice_memory_high less budget.test_reserve_mib is "
            f"below the installed test ceiling, {ceiling} MiB. Raise the first, lower the second, or install again",
        )
    held = ctx.check_places.take(places, session.name)
    if held is not None:
        who = f"{', '.join(held)} {'is' if len(held) == 1 else 'are'} running tests"
        room = f"the factory's memory holds {places} such check{'' if places == 1 else 's'} at a time"
        raise Refused("check-places", f"{who}, and {room}: run again when one ends")
    try:
        yield _count_run(ctx, session, spend=True) if counted else None
    finally:
        ctx.check_places.give_back(session.name)


def _tail(text: str, kib: int) -> str:
    data = text.encode()
    if len(data) <= kib * 1024:
        return text
    return "[the start of the report is cut; the end follows]\n" + data[-kib * 1024 :].decode(errors="replace")


def check(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    if args.get("list"):
        return ok(checks=[{"id": c, "rule": catalog.load(c).rule} for c in catalog.IDS])
    session = _session(ctx)
    only = _only(args)
    checkout = session.worktree
    commit, tree, branch, dirty = _state(ctx, checkout)
    if dirty:
        raise Refused("check", "commit your changes first: a check reads what the branch has committed")
    on_default = branch == ctx.settings().get_str("repo.default_branch")
    # A pass is a branch's check against the default branch it left, every ratchet included. --all on a branch skips
    # the ratchets (they compare only what a branch changed): it is a run of the whole set, held to the merge turn
    # and counted against it like any, but it records nothing the merge would trust.
    whole = only is None
    records = whole and not (args.get("all") and not on_default)
    if records and passed(ctx.places, tree):
        return ok(exit=0, passed=True, tree=tree, output=f"factory check: tree {tree} already passed; nothing to run")
    with _place(ctx, session, only, whole and session.role == IMPL) as left:
        argv = ["--only", ",".join(only)] if only is not None else []
        if args.get("all"):
            argv.append("--all")
        elif on_default:
            argv += _since(ctx, checkout)
        code, output = ctx.launcher.run_check(checkout, argv)
    recorded = False
    if records and code == 0:
        after_commit, after_tree, _, after_dirty = _state(ctx, checkout)
        if (after_commit, after_tree, after_dirty) == (commit, tree, False):
            _record(ctx, tree, commit, branch, session.task)
            recorded = True
            output += f"factory check: recorded tree {tree} as passed\n"
        else:
            output += "factory check: the checkout moved while it was checked, so no pass is recorded\n"
    elif not records and code == 0:
        why = "--only ran part of the set" if only is not None else "--all on a branch runs no ratchet"
        output += f"factory check: {why}, so no pass is recorded\n"
    if left is not None:
        output += f"factory check: {left} whole runs left on this merge turn\n"
    kib = ctx.settings().get_int("checks.output_kib")
    return ok(exit=code, passed=recorded, tree=tree, output=_tail(output, kib))
