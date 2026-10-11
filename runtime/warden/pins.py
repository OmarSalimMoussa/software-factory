"""The owner's moves of the product to an upgraded factory: commits only root makes, at an upgrade the owner asked
for, and the one way a change to .github/factory-ref or the CI workflows ships.

A push lets a pin's own paths through when nothing else in the range changed them, before or after it; a pin's tree
counts as checked when its parent's tree passed, since it changes nothing a check reads. The warden pushes a pin
itself, once it is the newest commit that may ship, trying again every watch.branch_retry_minutes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from runtime.shared import store, upgrades
from runtime.shared.paths import Places
from runtime.warden import checks
from runtime.warden.context import Context

Git = Callable[..., str]
PIN_PATHS = (".github/factory-ref", ".github/workflows/")


def _pushed_path(places: Places) -> Path:
    return places.state / "pins-pushed.json"


def _changed(git: Git, commit: str) -> set[str]:
    return {p for p in git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit).split("\0") if p.strip()}


def _pin(git: Git, places: Places, commit: str) -> set[str] | None:
    """The paths a pin changes; None for a commit that is not one, or that changes anything else."""
    if commit not in upgrades.read_pins(places):
        return None
    paths = _changed(git, commit)
    return paths if all(p.startswith(PIN_PATHS) for p in paths) else None


def allowed(git: Git, places: Places, remote: str, head: str) -> set[str]:
    """The protected paths the pins in remote..head may change, each changed by nothing else in the range."""
    found: set[str] = set()
    for commit in git("rev-list", f"{remote}..{head}").split():
        paths = _pin(git, places, commit)
        if not paths:
            continue
        before = git("diff", "--name-only", remote, f"{commit}^", "--", *paths)
        after = git("diff", "--name-only", commit, head, "--", *paths)
        if not before.strip() and not after.strip():
            found |= paths
    return found


def tree_passes(git: Git, places: Places, commit: str) -> bool:
    parent_tree = f"{commit}^^{{tree}}"  # the tree of the pin's parent
    return _pin(git, places, commit) is not None and checks.passed(places, git("rev-parse", parent_tree).strip())


def push_pending(ctx: Context) -> None:
    """The warden's pins pass: push each pin not yet on GitHub, as the owner's upgrade asked."""
    from runtime.warden import github_push  # it reads this module's rules when it pushes

    places = ctx.places
    record = store.read_json_or(_pushed_path(places), {"pushed": [], "tried_at": None})
    waiting = [c for c in upgrades.read_pins(places) if c not in record["pushed"]]
    every = ctx.settings().get_int("watch.branch_retry_minutes") * 60
    if not waiting or (record["tried_at"] is not None and ctx.clock.now() - record["tried_at"] < every):
        return
    pushed = list(record["pushed"])
    try:
        for commit in waiting:
            github_push.push_checked(ctx, {"sha": commit}, None)
            pushed.append(commit)
    finally:
        store.write_json(_pushed_path(places), {"pushed": pushed, "tried_at": ctx.clock.now()}, mode=0o600)
