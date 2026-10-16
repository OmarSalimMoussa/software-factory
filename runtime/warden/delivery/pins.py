"""The commits only root makes for the owner, and the one way a change to a path only a person changes ships: an
upgrade's move of the product to its factory (a pin), and the owner's own change, which root commits on the owner's
command (``factory-own``).

A push lets such a commit's own paths through when nothing but these commits changes them in the range. A pin's tree
counts as checked when its parent's tree passed, since it changes nothing a check reads, so the warden's offer of the
staging push (github_push.py) takes a pin once it may ship. The owner's own change may change anything, so it ships
only with a tree that passed its own check.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from runtime.shared import upgrades
from runtime.shared.paths import Places
from runtime.warden import checks

Git = Callable[..., str]


def _changed(git: Git, commit: str) -> set[str]:
    return {p for p in git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit).split("\0") if p.strip()}


def _vouched(git: Git, records: list[dict[str, Any]], commit: str) -> set[str] | None:
    """The paths a commit root recorded changes, on its parent and with exactly its blobs; None for any other commit."""
    record = next((r for r in records if r["commit"] == commit), None)
    if record is None or git("rev-parse", f"{commit}^").strip() != record["parent"]:
        return None
    paths = _changed(git, commit)
    listed = git("ls-tree", "-r", commit, "--", *record["blobs"]).splitlines()
    held = {line.split("\t", 1)[1]: line.split()[2] for line in listed if "\t" in line}
    wanted = {path: blob for path, blob in record["blobs"].items() if blob}
    return paths if paths and paths <= set(record["blobs"]) and held == wanted else None


def _pin(git: Git, places: Places, commit: str) -> set[str] | None:
    return _vouched(git, upgrades.read_pins(places), commit)


def allowed(git: Git, places: Places, remote: str, head: str) -> set[str]:
    """The protected paths root's commits in remote..head change, when nothing but those commits changes them in the
    range: an upgrade's pin may sit on the one before it, and the owner's change beside either, none pushed yet."""
    records = upgrades.read_pins(places) + upgrades.read_owned(places)
    line = git("rev-list", "--reverse", "--first-parent", f"{remote}..{head}").split()
    vouched = [(commit, paths) for commit in line if (paths := _vouched(git, records, commit))]
    if not vouched:
        return set()
    found = set().union(*(paths for _, paths in vouched))
    starts, ends = [remote, *(c for c, _ in vouched)], [*(f"{c}^" for c, _ in vouched), head]
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
