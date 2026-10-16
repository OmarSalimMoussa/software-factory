"""The commits only root makes on the agents' default branch for the owner: an upgrade's move of the product to its
factory, and the owner's own change (``factory-own``). Root writes each file's text itself, with no hook and no filter
of the agent's, works out each blob's id itself, and records them, so the warden's push lets exactly that through
(runtime/warden/delivery/pins.py)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.shared import upgrades
from runtime.shared.paths import AGENT_USER
from runtime.system.host import Host

# The factory's git identity: the agents commit as it, and root's own commits name it on their command line.
GIT_NAME, GIT_EMAIL = "software factory", "factory@software.invalid"
AS_FACTORY = ["-c", f"user.name={GIT_NAME}", "-c", f"user.email={GIT_EMAIL}"]
_NEW_FILE = "100644"  # the mode of a file root adds; a file it changes keeps its own


class NotHere(Exception):
    """The checkout cannot take a commit now: off the default branch, or with changes of its own."""


@dataclass(frozen=True)
class Made:
    commit: str
    parent: str
    blobs: dict[str, str | None]


def git(checkout: Path) -> list[str]:
    """git in the agents' checkout with no hook or replace ref: nothing the agent set or planted shapes root's work."""
    return ["git", "-C", str(checkout), "-c", "core.hooksPath=/dev/null", "-c", "core.useReplaceRefs=false"]


def ready(host: Host, checkout: Path, branch: str) -> None:
    """NotHere unless the checkout is on ``branch`` with no tracked file changed and no operation half done."""
    on = host.query([*git(checkout), "symbolic-ref", "--short", "HEAD"], user=AGENT_USER)[1].strip()
    dirty = host.query([*git(checkout), "status", "--porcelain", "--untracked-files=no"], user=AGENT_USER)[1].strip()
    if on != branch:
        raise NotHere(f"the agents' checkout is on {on or 'no branch'}, not {branch}")
    if dirty:
        raise NotHere("the agents' checkout has changes of its own to tracked files")


def tip(host: Host, checkout: Path, branch: str) -> str:
    return host.run(
        [*git(checkout), "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"], user=AGENT_USER
    ).strip()


def commit(
    host: Host,
    checkout: Path,
    branch: str,
    parent: str,
    want: Mapping[str, str | None],
    message: str,
    env: Mapping[str, str],
    ledger: tuple[Path, list[dict[str, Any]]],
) -> Made | None:
    """One commit on ``parent``, the tip of ``branch``, setting each path of ``want`` to its text or removing it (None);
    None when the parent holds exactly that. Added to the ``ledger`` before the branch moves; the files follow."""
    run = git(checkout)
    listed = host.run([*run, "ls-tree", "-r", parent, "--", *want], user=AGENT_USER).splitlines()
    modes = {line.split("\t", 1)[1]: line.split()[0] for line in listed}
    held = set(modes)
    blobs = {rel: upgrades.blob_id(text) if text is not None else None for rel, text in want.items()}
    blobs = {rel: blob for rel, blob in blobs.items() if blob is not None or rel in held}
    index = {"GIT_INDEX_FILE": str(checkout / ".git" / "factory-root.index")}
    host.run([*run, "read-tree", parent], user=AGENT_USER, env=index)
    for rel, text in want.items():
        if text is not None:
            host.run([*run, "hash-object", "-w", "--no-filters", "--stdin"], user=AGENT_USER, stdin=text)
            entry = f"{modes.get(rel, _NEW_FILE)},{blobs[rel]},{rel}"
            host.run([*run, "update-index", "--add", "--cacheinfo", entry], user=AGENT_USER, env=index)
        elif rel in held:
            host.run([*run, "update-index", "--force-remove", rel], user=AGENT_USER, env=index)
    tree = host.run([*run, "write-tree"], user=AGENT_USER, env=index).strip()
    host.run(["rm", "-f", index["GIT_INDEX_FILE"]], user=AGENT_USER)
    if tree == host.run([*run, "rev-parse", f"{parent}^{{tree}}"], user=AGENT_USER).strip():
        return None
    argv = [*run, *AS_FACTORY, "commit-tree", tree, "-p", parent, "-m", message]
    made = host.run(argv, user=AGENT_USER, env=dict(env)).strip()
    listed = host.run([*run, "ls-tree", "-r", made, "--", *blobs], user=AGENT_USER).splitlines()
    if {line.split("\t", 1)[1]: line.split()[2] for line in listed} != {k: v for k, v in blobs.items() if v}:
        raise NotHere("the commit git made holds other files than root gave it")
    found = Made(made, parent, blobs)
    record(host, *ledger, found)  # first: a branch never holds a root commit no record vouches for
    host.run([*run, "update-ref", f"refs/heads/{branch}", made, parent], user=AGENT_USER)
    host.run([*run, "read-tree", "-m", "-u", parent, made], user=AGENT_USER)
    return found


def record(host: Host, path: Path, records: list[dict[str, Any]], made: Made) -> None:
    """Add ``made`` to root's record at ``path``, which the warden reads and no agent can write."""
    entry = {"commit": made.commit, "parent": made.parent, "blobs": made.blobs}
    host.write(path, json.dumps([*records, entry], indent=1) + "\n", mode=0o644)
