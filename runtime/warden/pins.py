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


def _pushed_path(places: Places) -> Path:
    return places.state / "pins-pushed.json"


def _changed(git: Git, commit: str) -> set[str]:
    return {p for p in git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit).split("\0") if p.strip()}


def _pin(git: Git, places: Places, commit: str) -> set[str] | None:
    """The paths a pin changes, when the commit is one root recorded, on its recorded parent, holding exactly the
    blobs root worked out for it; None for any other commit."""
    record = next((p for p in upgrades.read_pins(places) if p["commit"] == commit), None)
    if record is None or git("rev-parse", f"{commit}^").strip() != record["parent"]:
        return None
    paths = _changed(git, commit)
    listed = git("ls-tree", "-r", commit, "--", *record["blobs"]).splitlines()
    held = {line.split("\t", 1)[1]: line.split()[2] for line in listed if "\t" in line}
    wanted = {path: blob for path, blob in record["blobs"].items() if blob}
    return paths if paths and paths <= set(record["blobs"]) and held == wanted else None


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
    waiting = [p["commit"] for p in upgrades.read_pins(places) if p["commit"] not in record["pushed"]]
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
