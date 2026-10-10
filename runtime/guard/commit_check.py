"""What the product's git hooks check: inside the factory's slice, no commit or merge into the default branch changes
a protected path, nothing is committed to the factory's own repo, and no push leaves (the warden pushes). A merge
into the default branch lands only with a tree that passed `factory check` on its branch, so it is never checked
twice and never unchecked.

The guard reads each tool call, but a script run from the shell can write a file it never sees; every change reaches
the default branch through a commit or a merge, so the hooks check both. The hooks run this installed, root-owned
copy as `/usr/bin/python3 -I <install>/runtime/guard/commit_check.py <hook>`. Stdlib only.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.guard.facts import read_settings  # noqa: E402 - the path line above must run first
from runtime.guard.writes import is_protected, protected_paths  # noqa: E402
from runtime.shared.paths import INSTALL, SLICE, Places  # noqa: E402

# `factory check`'s own reader of its pass record: exit 0 when a tree passed, 1 when it did not.
PASSES = Path(__file__).resolve().parents[2] / "checks" / "runner" / "passes.py"


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
        return "commit-check: only the warden pushes; the orchestrator ships with `factory push <sha>`"
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


def passed(tree: str) -> bool:
    done = subprocess.run([sys.executable, "-I", str(PASSES), "check", tree], capture_output=True, text=True)
    if done.returncode not in (0, 1):
        raise HookError(f"the pass record cannot be read: {done.stderr.strip()[-200:]}")
    return done.returncode == 0


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
    # No cgroup file, no slice: a machine that runs no factory.
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
        reason = refusal(
            [p for p in paths if p],
            prefixes=protected_paths(settings),
            in_slice=_in_slice(),
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
        tree = _git("write-tree").strip()
        if not passed(tree):
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
