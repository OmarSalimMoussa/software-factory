"""Repo shape: the one shape every product takes, so the factory needs no per-product configuration.

GOAL.md with its four headings and its goal items, each a stable name and an outcome, read by the warden's own
parser; ``queue/`` ignored by git and never tracked; ``qa/scenarios/``; ``check-values/`` holding only files named for
what they feed; and an executable ``scripts/dev`` with the fixed verbs, which ``scripts/dev verbs`` lists.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from checks.runner import catalog
from checks.runner.repo import GitError, Repo
from runtime.queue import goal

ID = "repo-shape"
RULE = "The repo has the standard shape: files, headings, verbs, queue/ ignored"
VALUES_FILE = None
VALUES = None

HEADINGS = ("Who it is for", "The goal", "How it looks and feels", "How it speaks")
VERBS = ("up", "down", "empty", "health", "sign-in", "seed", "desktop", "verbs", "test")
OPTIONAL_VERBS = ("reports", "deps")
_DONE_WHEN = re.compile(r"^\S.*\. Done when \S.*\.$")


def goal_problems(repo: Repo) -> list[str]:
    if not (repo.root / "GOAL.md").is_file():
        return ["GOAL.md is missing: a person writes it, with the four headings " + ", ".join(HEADINGS)]
    text = repo.read("GOAL.md")
    found = re.findall(r"^## (.+?)\s*$", text, re.M)
    out = [f"GOAL.md has no '## {h}' heading" for h in HEADINGS if h not in found]
    out += [f"GOAL.md has '## {h}' {found.count(h)} times" for h in HEADINGS if found.count(h) > 1]
    if "The goal" not in found:
        return out
    try:
        items = goal.parse(text)
    except goal.GoalError as exc:
        return [*out, *exc.problems]
    return out + [f"GOAL.md: {i.id}'s outcome does not read '<outcome>. Done when <check>.'" for i in items
                  if not _DONE_WHEN.match(i.text)]  # fmt: skip


def queue_problems(repo: Repo) -> list[str]:
    out = [
        f"{rel} is tracked: queue/ is the factory's working folder and is never committed"
        for rel in repo.tracked
        if rel.startswith("queue/")
    ]
    if not (repo.root / ".gitignore").is_file():
        return [*out, ".gitignore is missing: it lists queue/"]
    try:
        repo.git("check-ignore", "-q", "--no-index", "queue/request.md")
    except GitError:
        out.append(".gitignore does not ignore queue/: add the line queue/")
    return out


def folder_problems(repo: Repo) -> list[str]:
    out = [
        f"{folder}/ is missing or holds no tracked file"
        for folder in ("qa/scenarios", "check-values")
        if not any(rel.startswith(f"{folder}/") for rel in repo.tracked)
    ]
    known = catalog.values_files()
    for rel in repo.tracked:
        if rel.startswith("check-values/") and rel.removeprefix("check-values/") not in known:
            out.append(f"{rel} feeds no standard check: check-values/ holds only " + ", ".join(sorted(known)))
    return out


def executable(repo: Repo, rel: str) -> list[str]:
    path = repo.root / rel
    if rel not in repo.tracked:
        return [f"{rel} is missing (or not committed)"]
    return [] if os.access(path, os.X_OK) else [f"{rel} is not executable: chmod +x it and commit the mode"]


def script_problems(repo: Repo) -> list[str]:
    dev = executable(repo, "scripts/dev")
    if dev:
        return dev
    done = subprocess.run(
        [str(repo.root / "scripts" / "dev"), "verbs"], cwd=repo.root, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        return [f"scripts/dev verbs exited {done.returncode}: {done.stderr.strip()[-300:]}"]
    listed = done.stdout.split()
    out = [f"scripts/dev verbs does not list {verb}: every product supports it" for verb in VERBS if verb not in listed]
    out += [
        f"scripts/dev verbs lists {verb}, which is not a standard verb: the factory never calls it"
        for verb in listed
        if verb not in VERBS + OPTIONAL_VERBS
    ]
    return out


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    return goal_problems(repo) + queue_problems(repo) + folder_problems(repo) + script_problems(repo)
