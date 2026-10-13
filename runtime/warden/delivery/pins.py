"""The owner's moves of the product to an upgraded factory: commits only root makes, at an upgrade the owner asked
for, and the one way a change to .github/factory-ref or the CI workflows ships.

A push lets a pin's own paths through when nothing but pins changes them in the range; a pin's tree
counts as checked when its parent's tree passed, since it changes nothing a check reads. The warden asks the owner
to push a pin once it may ship, as the orchestrator asks, asking again every watch.branch_retry_minutes until GitHub
holds it.
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
    """The paths a pin root recorded changes, on its parent and with exactly its blobs; None for any other commit."""
    record = next((p for p in upgrades.read_pins(places) if p["commit"] == commit), None)
    if record is None or git("rev-parse", f"{commit}^").strip() != record["parent"]:
        return None
    paths = _changed(git, commit)
    listed = git("ls-tree", "-r", commit, "--", *record["blobs"]).splitlines()
    held = {line.split("\t", 1)[1]: line.split()[2] for line in listed if "\t" in line}
    wanted = {path: blob for path, blob in record["blobs"].items() if blob}
    return paths if paths and paths <= set(record["blobs"]) and held == wanted else None


def allowed(git: Git, places: Places, remote: str, head: str) -> set[str]:
    """The protected paths the pins in remote..head change, when nothing but those pins changes them in the range:
    an upgrade's pin may sit on the one before it, both not pushed yet."""
    line = git("rev-list", "--reverse", "--first-parent", f"{remote}..{head}").split()
    pinned = [(commit, paths) for commit in line if (paths := _pin(git, places, commit))]
    if not pinned:
        return set()
    found = set().union(*(paths for _, paths in pinned))
    starts, ends = [remote, *(c for c, _ in pinned)], [*(f"{c}^" for c, _ in pinned), head]
    segments = zip(starts, ends, strict=True)
    if any(git("diff", "--name-only", start, end, "--", *found).strip() for start, end in segments):
        return set()
    return found


def tree_passes(git: Git, places: Places, commit: str) -> bool:
    """A pin's tree counts as checked when its parent's passed, or its parent is a pin that counts: an upgrade's pin
    may sit on the one before it, not pushed yet."""
    if _pin(git, places, commit) is None:
        return False
    parent = git("rev-parse", f"{commit}^").strip()
    return checks.passed(places, git("rev-parse", f"{parent}^{{tree}}").strip()) or tree_passes(git, places, parent)


def ask_pending(ctx: Context) -> None:
    from runtime.warden.delivery import github_push  # it reads this module's rules when it pushes

    places = ctx.places
    record = store.read_json_or(_pushed_path(places), {"pushed": [], "tried_at": None})
    waiting = [p["commit"] for p in upgrades.read_pins(places) if p["commit"] not in record["pushed"]]
    every = ctx.settings().get_int("watch.branch_retry_minutes") * 60
    if not waiting or (record["tried_at"] is not None and ctx.clock.now() - record["tried_at"] < every):
        return
    pushed = list(record["pushed"])
    try:
        for commit in waiting:
            if not github_push.ask_push(ctx, {"sha": commit}, None)["commits"]:
                pushed.append(commit)
    finally:
        store.write_json(_pushed_path(places), {"pushed": pushed, "tried_at": ctx.clock.now()}, mode=0o600)
