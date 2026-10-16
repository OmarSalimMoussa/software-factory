"""factory-own, run with sudo by the owner: their own change to a path only a person changes (GOAL.md, factory.toml,
a script it names), which root commits on the agents' default branch and records, so the warden's push lets it
through once a check of that branch passes (runtime/warden/delivery/github_push.py). Root applies a patch outside any
repository, to the files as the branch holds them, so no hook, filter or setting of the agent's shapes it. It
refuses, changing nothing, while a task holds the merge turn, while the checkout is off the branch or has changes of
its own, and for a patch that renames, copies, holds a binary or makes a file anything but a plain one. Stdlib only.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

# `python3 -I` leaves the script's own folder off sys.path; the install's root is root-owned, so it is safe to add.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.queue import turn
from runtime.shared import environment, store, upgrades
from runtime.shared.paths import AGENT_USER, Places
from runtime.system import root_commits
from runtime.system.host import CommandError, Host
from runtime.system.journal import Journal
from runtime.system.real_host import RealHost

_HEADER = re.compile(r"^(Author|Email|Subject|Date): ?(.*)$")
_PLAIN = re.compile(r"^ (create|delete) mode 100644 ")


class Refusal(Exception):
    """Why nothing was changed, in the owner's words."""


def _apply(diff: str, parent_texts: dict[str, str | None]) -> dict[str, str | None]:
    """Each path's text after root applies ``diff`` to the parent's texts in a folder of its own; None if removed."""
    with tempfile.TemporaryDirectory(prefix="factory-own-") as folder:
        root = Path(folder)
        for rel, text in parent_texts.items():
            if text is not None:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text, encoding="utf-8")
        _git(["apply"], root, diff)
        try:
            return {rel: (root / rel).read_bytes().decode() if (root / rel).exists() else None for rel in parent_texts}
        except UnicodeDecodeError as exc:
            raise Refusal(f"the patch leaves a file that is not UTF-8 text ({exc.reason})") from exc


def _git(argv: list[str], cwd: Path, stdin: str | None = None) -> str:
    """Root's own git outside any repository: the folder above ``cwd`` is a ceiling no lookup passes."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GIT_CEILING_DIRECTORIES": str(cwd.parent), "HOME": str(cwd)}
    done = subprocess.run(["git", *argv], cwd=cwd, env=env, input=stdin, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise Refusal(f"git {argv[0]} refuses the patch: {done.stderr.strip()[-300:]}")
    return done.stdout


def read_patch(patch: Path) -> tuple[dict[str, str], str, list[str], str]:
    """The patch's author (as git's environment names it), message, paths and diff, read once; Refusal if refused."""
    try:
        text = patch.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise Refusal(f"{patch} cannot be read as text ({type(exc).__name__})") from exc
    with tempfile.TemporaryDirectory(prefix="factory-own-") as folder:
        here = Path(folder)
        count = _git(["mailsplit", f"-o{here}"], here, text).strip()
        if count != "1":
            raise Refusal(f"{patch} holds {count} commits: give factory-own one patch for each")
        info = _git(["mailinfo", "body", "diff"], here, (here / "0001").read_text(encoding="utf-8"))
        body, diff = (here / "body").read_text(encoding="utf-8"), (here / "diff").read_text(encoding="utf-8")
        summary = _git(["apply", "--summary"], here, diff).splitlines()
        stats = _git(["apply", "--numstat", "-z"], here, diff).split("\0")
    head = {m.group(1): m.group(2) for line in info.splitlines() if (m := _HEADER.match(line))}
    if not head.get("Author") or not head.get("Email") or not head.get("Subject"):
        raise Refusal(f"{patch} is not a patch `git format-patch` wrote: it names no author or subject")
    odd = [line.strip() for line in summary if line.strip() and not _PLAIN.match(line)]
    if odd:
        raise Refusal(
            f"{patch} renames, copies or changes a mode ({odd[0]}): make it with --no-renames, plain files only"
        )
    paths = []
    for row in filter(None, stats):
        added, removed, rel = row.split("\t", 2)
        if added == "-" or removed == "-":
            raise Refusal(f"{patch} holds a binary file, {rel}")
        if PurePosixPath(rel).is_absolute() or ".." in PurePosixPath(rel).parts:
            raise Refusal(f"{patch} names a path outside the checkout, {rel}")
        paths.append(rel)
    if not paths:
        raise Refusal(f"{patch} changes no file")
    author = {"GIT_AUTHOR_NAME": head["Author"], "GIT_AUTHOR_EMAIL": head["Email"]}
    if head.get("Date"):
        author["GIT_AUTHOR_DATE"] = head["Date"]
    message = head["Subject"] + ("\n\n" + body.strip() if body.strip() else "")
    return author, message, paths, diff


def from_patch(host: Host, places: Places, checkout: Path, branch: str, patch: Path) -> str:
    author, message, paths, diff = read_patch(patch)
    if turn.holder(places) is not None:
        raise Refusal("a task holds the merge turn, and the default branch stays still until it merges: try again then")
    try:
        root_commits.ready(host, checkout, branch)
    except root_commits.NotHere as exc:
        raise Refusal(str(exc)) from exc
    run, parent = root_commits.git(checkout), root_commits.tip(host, checkout, branch)
    before: dict[str, str | None] = {}
    for rel in paths:
        code, blob = host.query([*run, "rev-parse", "--verify", "--quiet", f"{parent}:{rel}"], user=AGENT_USER)
        if code != 0:
            before[rel] = None
            continue
        text = host.run([*run, "cat-file", "blob", blob.strip()], user=AGENT_USER)
        # The host reads output as text, which turns \r\n into \n: a file that does not come back as its own blob
        # would be committed with other bytes than the default branch holds.
        if upgrades.blob_id(text) != blob.strip():
            raise Refusal(f"{rel} has Windows line ends or bytes that are not UTF-8, which root cannot carry over")
        before[rel] = text
    want = _apply(diff, before)
    ledger = (upgrades.owned_path(places), upgrades.read_owned(places))
    made = root_commits.commit(host, checkout, branch, parent, want, message, author, ledger)
    if made is None:
        raise Refusal(f"{branch} already holds this change")
    return made.commit


def from_commit(host: Host, places: Places, checkout: Path, branch: str, given: str) -> str:
    run = root_commits.git(checkout)
    code, found = host.query([*run, "rev-parse", "--verify", f"{given}^{{commit}}"], user=AGENT_USER)
    commit = found.strip()
    if code != 0:
        raise Refusal(f"the agents' checkout holds no commit {given}")
    if commit not in host.run([*run, "rev-list", "--first-parent", branch], user=AGENT_USER).split():
        raise Refusal(f"{commit[:12]} is not on {branch} in the agents' checkout")
    parents = host.run([*run, "rev-list", "--parents", "-n", "1", commit], user=AGENT_USER).split()[1:]
    if len(parents) != 1:
        raise Refusal(f"{commit[:12]} is a merge or the first commit: name the change itself")
    if any(r["commit"] == commit for r in upgrades.read_owned(places)):
        raise Refusal(f"{commit[:12]} is already recorded as yours")
    paths = host.run([*run, "diff-tree", "--no-commit-id", "--no-renames", "--name-only", "-r", "-z", commit],
                     user=AGENT_USER).split("\0")  # fmt: skip
    paths = [p for p in paths if p]
    listed = host.run([*run, "ls-tree", "-r", commit, "--", *paths], user=AGENT_USER).splitlines()
    held = {line.split("\t", 1)[1]: line.split()[2] for line in listed if "\t" in line}
    made = root_commits.Made(commit, parents[0], {rel: held.get(rel) for rel in paths})
    root_commits.record(host, upgrades.owned_path(places), upgrades.read_owned(places), made)
    return commit


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="factory-own", description="Commit or record the owner's own change.")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--patch", type=Path, help="a file `git format-patch` wrote, of one commit")
    which.add_argument("--commit", help="a commit already on the default branch of the agents' checkout")
    args = parser.parse_args(argv)
    if os.geteuid() != 0 or not environment.sudo_user():
        print("factory-own: run it with sudo from your own account", file=sys.stderr)
        return 2
    places, host = Places.system(), RealHost(Journal(terminal=False))
    try:
        settings = upgrades.installed_settings(places)
        checkout, branch = Path(settings.get_str("repo.path")), settings.get_str("repo.default_branch")
        if args.patch is not None:
            commit = from_patch(host, places, checkout, branch, args.patch)
        else:
            commit = from_commit(host, places, checkout, branch, args.commit)
    except (Refusal, root_commits.NotHere) as exc:
        print(f"factory-own: stopped, nothing changed: {exc}", file=sys.stderr)
        return 1
    except (CommandError, store.StoreError) as exc:
        print(f"factory-own: stopped: {exc}", file=sys.stderr)
        return 1
    print(f"factory-own: {commit[:12]} on {branch} is recorded as yours. The warden asks the orchestrator to check "
          f"{branch}; once that passes, your change waits on Home with the next push to staging.")  # fmt: skip
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
