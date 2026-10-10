"""`factory check`: run the standard set on the product checkout holding the current folder.

By default it checks what the branch adds to the default branch (committed changes only, since a pass is recorded
for HEAD's tree) and, when every check ran and passed, records that tree as passed. ``--all`` checks everything (CI).
``--only <ids>`` runs the named checks and records nothing. ``--list`` names every check and its rule.
Exit 0 when every check passed, 1 when one failed, 2 when the run could not start.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from checks.runner import catalog, passes
from checks.runner.catalog import NOTE, Check
from checks.runner.repo import GitError, Repo, git
from checks.runner.values import ValuesError
from runtime.shared import settings as settings_mod
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.settings import Settings, SettingsError

LAST = "tests"  # the heaviest check runs alone, after the others
DEPS = "deps"  # the optional scripts/dev verb that installs what the product's checks and tests read


class Refusal(Exception):
    """The run cannot start; the message says why and what to do."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory check", description="Run the standard checks.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="check everything, not only what the branch adds")
    group.add_argument("--list", action="store_true", help="name every check and its rule")
    parser.add_argument("--only", metavar="IDS", help="run only these checks (comma-separated); records no pass")
    return parser


def on_ci() -> bool:
    """CI runners hold nothing else, so a test run there needs no memory guard; CI sets CI=true."""
    return os.environ.get("CI") == "true"


def effective_settings() -> Settings | None:
    """The machine's effective settings, which the warden publishes; None on CI, where only --all runs and nothing
    else shares the machine, so a test run needs no memory guard."""
    if on_ci():
        return None
    return settings_mod.build(store.read_json(Places.system().state / "public" / "settings.json"), {})


def chosen(only: str | None) -> list[str]:
    if only is None:
        return list(catalog.IDS)
    wanted = [name.strip() for name in only.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in catalog.IDS]
    if unknown or not wanted:
        raise Refusal(f"no check named {', '.join(unknown) or '(none)'}; the checks: {', '.join(catalog.IDS)}")
    return [check_id for check_id in catalog.IDS if check_id in wanted]


def changed_paths(root: Path, settings: Settings | None) -> tuple[str, ...] | None:
    """What the branch changed since it left the default branch, or None when it adds nothing."""
    if settings is None:
        raise Refusal("on CI, run factory check --all")
    if git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise Refusal("commit your changes first: factory check checks what the branch has committed")
    branch = settings.get_str("repo.default_branch")
    try:
        base = git(root, "merge-base", "HEAD", branch).strip()
    except GitError as exc:
        raise Refusal(f"cannot find where this branch left {branch}: {exc}") from exc
    if base == git(root, "rev-parse", "HEAD").strip():
        return None
    return tuple(p for p in git(root, "diff", "--name-only", "-z", base, "HEAD").split("\0") if p)


def run_one(check_id: str, repo: Repo) -> list[str]:
    """One check's problems; a check that cannot run fails with the reason, never passes."""
    try:
        check: Check = catalog.load(check_id)
        values = repo.values(check.values_file, check.values) if check.values_file else {}
        return check.run(repo, values)
    except ValuesError as exc:
        return exc.problems
    except Exception as exc:  # one check's fault must not hide the others' verdicts; it fails, loudly
        return [f"the check could not run: {type(exc).__name__}: {exc}"]


def report(results: dict[str, list[str]], out: TextIO) -> list[str]:
    failed = []
    for check_id, lines in results.items():
        problems = [line for line in lines if not line.startswith(NOTE)]
        out.write(f"{'FAIL' if problems else 'ok  '}  {check_id}\n")
        for line in lines:
            out.write("".join(f"      {part}\n" for part in line.splitlines()))
        if problems:
            failed.append(check_id)
    return failed


def run(argv: list[str], cwd: Path, settings: Settings | None, out: TextIO) -> int:
    args = _parser().parse_args(argv)
    if args.list:
        for check_id in catalog.IDS:
            out.write(f"{check_id:20s} {catalog.load(check_id).rule}\n")
        return 0
    try:
        ids = chosen(args.only)
        root = Path(git(cwd, "rev-parse", "--show-toplevel").strip())
        changed = None if args.all else changed_paths(root, settings)
    except (Refusal, GitError) as exc:
        out.write(f"factory check: {exc}\n")
        return 2
    if not args.all and changed is None:
        out.write(
            "factory check: HEAD adds nothing to the default branch, so there is nothing to check "
            "(--all checks everything)\n"
        )
        return 0
    unprepared = prepare(root)
    if unprepared:
        out.write(f"factory check: {unprepared}\n")
        return 1
    repo = Repo.open(root, changed)
    if settings is not None and not on_ci():
        ceiling = settings.get_int("budget.test_memory_ceiling_mib")
        repo.test_memory = (ceiling, settings.get_int("budget.test_reserve_mib"))
    checked_tree = git(root, "rev-parse", "HEAD^{tree}").strip()
    started = time.monotonic()
    scope = "everything" if changed is None else f"{len(changed)} changed path(s)"
    out.write(f"factory check: {len(ids)} check(s) over {scope}\n")
    first = [check_id for check_id in ids if check_id != LAST]
    with ThreadPoolExecutor(max_workers=os.cpu_count()) as pool:
        found = dict(zip(first, pool.map(lambda check_id: run_one(check_id, repo), first), strict=True))
    if LAST in ids:
        found[LAST] = run_one(LAST, repo)
    failed = report({check_id: found[check_id] for check_id in ids}, out)
    seconds = time.monotonic() - started
    if failed:
        out.write(f"factory check: {len(failed)} of {len(ids)} failed in {seconds:.0f}s: {', '.join(failed)}\n")
        return 1
    out.write(f"factory check: {len(ids)} passed in {seconds:.0f}s\n")
    _record(root, checked_tree, args.only is not None, out)
    return 0


def prepare(root: Path) -> str:
    """Runs the product's ``scripts/dev deps`` first when it has one, since several checks read what it installs (a
    product's own type checker, its package trees); "" when done or not offered, else why the checks cannot start."""
    dev = root / "scripts" / "dev"
    if not os.access(dev, os.X_OK):
        return ""
    listed = subprocess.run([str(dev), "verbs"], cwd=root, capture_output=True, text=True, check=False)
    if listed.returncode != 0 or DEPS not in listed.stdout.split():
        return ""
    done = subprocess.run([str(dev), DEPS], cwd=root, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip())[-1500:]
        return f"scripts/dev deps failed (exit {done.returncode}), so no check can read the product's packages: {said}"
    return ""


def _record(root: Path, tree: str, partial: bool, out: TextIO) -> None:
    """Record ``tree`` as passed, only when the run covered the whole set and read exactly that tree."""
    if partial:
        out.write("factory check: --only ran part of the set, so no pass is recorded\n")
        return
    dirty = git(root, "status", "--porcelain", "--untracked-files=no").strip()
    if dirty or git(root, "rev-parse", "HEAD^{tree}").strip() != tree:
        out.write("factory check: the worktree is not the committed tree it checked, so no pass is recorded\n")
        return
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    passes.record(root, tree, branch, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    out.write(f"factory check: recorded tree {tree} as passed\n")


def main(argv: list[str]) -> int:
    try:
        settings = effective_settings()
    except (FileNotFoundError, store.StoreError, SettingsError) as exc:
        print(f"factory check: cannot read the factory's settings: {exc}", file=sys.stderr)
        return 2
    return run(argv, Path.cwd(), settings, sys.stdout)
