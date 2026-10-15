"""What the product's git hooks check: inside the factory's slice, no commit or merge into the default branch changes
a protected path, nothing is committed to the factory's own repo, and no push leaves (the warden pushes). A merge
into the default branch lands only with a tree that passed `factory check` on its branch, so it is never checked
twice and never unchecked. The protected paths are the guard's, scripts the main checkout's factory.toml names
included; while that file cannot be read, nothing is committed inside the slice.

The guard reads each tool call, but a script run from the shell can write a file it never sees; every change reaches
the default branch through a commit or a merge, so the hooks check both. The hooks run this installed, root-owned
copy as `/usr/bin/python3 -I <install>/runtime/guard/commit_check.py <hook>`. Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.guard.facts import read_settings
from runtime.guard.writes import is_protected, protected_paths
from runtime.shared.paths import INSTALL, SLICE, Places

UNNAMED = (
    "commit-check: git asks before it records the branch this merges, so the merge turn is checked when the merge is "
    "committed; the merge is staged: finish it with `git commit --no-edit`"
)


class HookError(Exception):
    """What the hook must read cannot be read; the commit is refused with the reason."""


def protected_hits(paths: list[str], prefixes: tuple[str, ...]) -> list[str]:
    return sorted({path for path in paths if is_protected(path, prefixes)})


def is_factory_repo(remote_url: str) -> bool:
    """Whether a remote is the factory's own repo, which the factory never works on."""
    name = remote_url.strip().rstrip("/").removesuffix(".git")
    return name.endswith(("/" + INSTALL.name, ":" + INSTALL.name))


def refusal(
    paths: list[str],
    *,
    prefixes: tuple[str, ...],
    in_slice: bool,
    hook: str,
    on_default: bool,
    merging: bool,
    factory: bool,
) -> str:
    """Why this commit, merge or push may not be made, or nothing.

    A merge into another branch is let through: it brings only what the default branch already holds, which was
    checked when it got there.
    """
    if not in_slice:
        return ""
    if hook == "pre-push":
        return "commit-check: only the owner pushes, from Home; your work stays local"
    if factory:
        return "commit-check: the factory never works on its own repo; only the owner changes it, by hand"
    hits = protected_hits(paths, prefixes) if on_default or not merging else []
    if not hits:
        return ""
    shown = ", ".join(hits[:5]) + (f" and {len(hits) - 5} more" if len(hits) > 5 else "")
    return (
        f"commit-check: {shown} only a person changes; take them out of this commit and file a request "
        "with `factory request new` for what you wanted changed"
    )


def passed(places: Places, tree: str) -> bool:
    """Whether the warden recorded ``tree`` as passed; it alone writes the trees it publishes."""
    path = places.public_check_passes
    try:
        trees = json.loads(path.read_text(encoding="utf-8"))["trees"]
    # telemetry: expected — no check has passed on this machine yet
    except FileNotFoundError:
        return False
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HookError(f"the warden's passed trees cannot be read ({type(exc).__name__})") from exc
    return isinstance(trees, list) and tree in trees


def turn_refusal(places: Places, merging: str) -> str:
    """Why a merge of ``merging`` into the default branch may not land: only the branch holding the merge turn, as
    the warden published it, merges; "" when it holds it."""
    path = places.public_turn
    try:
        branch = json.loads(path.read_text(encoding="utf-8"))["branch"]
    # telemetry: surfaced — no turn file: no task holds the turn, which `turn_refusal` says to the session
    except FileNotFoundError:
        return "commit-check: no task holds the merge turn; give one with `factory turn next`, then merge it"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HookError(f"the merge turn cannot be read ({type(exc).__name__})") from exc
    head = _optional_git("rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    if not head or head != merging:
        return f"commit-check: only {branch}, which holds the merge turn, merges now; this is another branch's head"
    return ""


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise HookError(f"git {args[0]} failed: {done.stderr.strip()[-200:]}")
    return done.stdout


def _optional_git(*args: str) -> str:
    done = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else ""


def _in_slice() -> bool:
    try:
        cgroup = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    # telemetry: expected — no cgroup file, no slice: a machine that runs no factory
    except OSError:
        return False
    return any(f"/{SLICE}/" in line + "/" for line in cgroup.splitlines())


def main(argv: list[str]) -> int:
    hook = argv[0] if argv else "pre-commit"
    try:
        branch = _optional_git("symbolic-ref", "--short", "-q", "HEAD")
        merging = hook == "pre-merge-commit" or bool(_optional_git("rev-parse", "-q", "--verify", "MERGE_HEAD"))
        # The effective settings the warden publishes, read as the guard reads them.
        settings = read_settings(Places.system())
        default = settings.get_str("repo.default_branch")
        paths = [] if hook == "pre-push" else _git("diff", "--cached", "--name-only", "--no-renames", "-z").split("\0")
        factory = is_factory_repo(_optional_git("remote", "get-url", "origin"))
        in_slice = _in_slice()
        # Read only inside the slice: the owner's own commit, which may be the fix to factory.toml, never needs them.
        reason = refusal(
            [p for p in paths if p],
            prefixes=protected_paths(settings) if in_slice else (),
            in_slice=in_slice,
            hook=hook,
            on_default=branch == default,
            merging=merging,
            factory=factory,
        )
        if reason:
            print(reason, file=sys.stderr)
            return 1
        if hook == "pre-push" or branch != default or not merging:
            return 0
        if in_slice:
            # pre-merge-commit runs before git records MERGE_HEAD; refused, the merge stays staged for `git commit`.
            head = _optional_git("rev-parse", "-q", "--verify", "MERGE_HEAD")
            turn = turn_refusal(Places.system(), head) if head else UNNAMED
            if turn:
                print(turn, file=sys.stderr)
                return 1
        tree = _git("write-tree").strip()
        if not passed(Places.system(), tree):
            print(
                f"commit-check: this merge's tree {tree} never passed `factory check`; the branch rebases onto "
                f"{default} and runs `factory check`, then this merges",
                file=sys.stderr,
            )
            return 1
    except Exception as exc:  # noqa: BLE001 - any failure refuses the commit, with its reason
        print(f"commit-check: refused, since {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"check: this merge's tree {tree} passed `factory check` on its branch; not checking it again")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
