"""Repo shape: the one shape every product takes, so the factory needs no per-product configuration.

GOAL.md with its four headings and a ranked G1..Gn checklist; ``queue/`` ignored by git and never tracked;
``qa/scenarios/``; ``check-values/`` holding only files named for what they feed; ``scripts/dev`` with the fixed
verbs, which ``scripts/dev verbs`` lists; and the four ``scripts/ci/`` steps, each executable.
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from checks.runner import catalog
from checks.runner.repo import GitError, Repo

ID = "repo-shape"
RULE = "The repo has the standard shape: files, headings, verbs, queue/ ignored"
VALUES_FILE = None
VALUES = None

HEADINGS = ("Who it is for", "The goal", "How it looks and feels", "How it speaks")
VERBS = ("up", "down", "empty", "health", "sign-in", "seed", "desktop", "verbs", "test")
OPTIONAL_VERBS = ("reports",)
CI_STEPS = ("build", "smoke", "deploy-staging", "promote")
_ITEM_RE = re.compile(r"^- G(\d+) \S.*\. Done when \S.*\.$")


def goal_problems(repo: Repo) -> list[str]:
    if not (repo.root / "GOAL.md").is_file():
        return ["GOAL.md is missing: a person writes it, with the four headings " + ", ".join(HEADINGS)]
    text = repo.read("GOAL.md")
    found = re.findall(r"^## (.+?)\s*$", text, re.M)
    out = [f"GOAL.md has no '## {h}' heading" for h in HEADINGS if h not in found]
    out += [f"GOAL.md has '## {h}' {found.count(h)} times" for h in HEADINGS if found.count(h) > 1]
    if "The goal" not in found:
        return out
    section = re.split(r"^## .*$", text.split("## The goal", 1)[1], maxsplit=1, flags=re.M)[0]
    items = [line.rstrip() for line in section.splitlines() if line.startswith("- ")]
    if not items:
        out.append("GOAL.md: '## The goal' has no checklist: each item reads '- G<n> <outcome>. Done when <check>.'")
    for n, line in enumerate(items, 1):
        match = _ITEM_RE.match(line)
        if match is None:
            out.append(f"GOAL.md: {line!r} does not read '- G<n> <outcome>. Done when <check>.'")
        elif int(match.group(1)) != n:
            out.append(f"GOAL.md: item {n} of the goal is numbered G{match.group(1)}: number them G1, G2, ... in rank")
    return out


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


def _executable(repo: Repo, rel: str) -> list[str]:
    path = repo.root / rel
    if rel not in repo.tracked:
        return [f"{rel} is missing (or not committed)"]
    return [] if os.access(path, os.X_OK) else [f"{rel} is not executable: chmod +x it and commit the mode"]


def script_problems(repo: Repo) -> list[str]:
    out = [p for step in CI_STEPS for p in _executable(repo, f"scripts/ci/{step}")]
    dev = _executable(repo, "scripts/dev")
    if dev:
        return dev + out
    done = subprocess.run(
        [str(repo.root / "scripts" / "dev"), "verbs"], cwd=repo.root, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        return [*out, f"scripts/dev verbs exited {done.returncode}: {done.stderr.strip()[-300:]}"]
    listed = done.stdout.split()
    out += [
        f"scripts/dev verbs does not list {verb}: every product supports it" for verb in VERBS if verb not in listed
    ]
    out += [
        f"scripts/dev verbs lists {verb}, which is not a standard verb: the factory never calls it"
        for verb in listed
        if verb not in VERBS + OPTIONAL_VERBS
    ]
    return out


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    return goal_problems(repo) + queue_problems(repo) + folder_problems(repo) + script_problems(repo)
