"""The goal items of the product's GOAL.md (factory spec, "GOAL.md"): one parser for the warden and the repo-shape
check.

Each item under `## The goal` has a stable name and an outcome: `- G-build-sop Build SOP works on prod. Done when
…`. A task names its item by that name, so moving, adding or removing lines never points it at another item. The
rank is the item's place among the open items, top first; an item checked done (`- [x] G-… …`) keeps its name, so
tasks that name it still read, but ranks no more. GOAL.md is the owner's file in a checkout another user owns, so it
is read without following a symlink.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from runtime.queue import files

GOAL_FILE = "GOAL.md"
HEADING = "## The goal"
# A goal item's stable name: G, then words of letters and digits joined by dashes (G1, G-build-sop).
NAME = r"G-?[0-9A-Za-z]{1,20}(?:-[0-9A-Za-z]{1,20}){0,5}"
_ITEM = re.compile(rf"^- (?:\[([ xX])\] )?({NAME}) (\S.*)$")


class GoalError(ValueError):
    """GOAL.md is missing its goal list, or an item cannot be read; ``problems`` lists every one."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True, slots=True)
class GoalItem:
    id: str
    text: str
    done: bool
    rank: int | None  # 1 for the first open item; None once done


def parse(text: str) -> list[GoalItem]:
    lines = text.split("\n")
    if HEADING not in lines:
        raise GoalError([f"{GOAL_FILE} has no {HEADING} heading"])
    items: list[GoalItem] = []
    problems: list[str] = []
    for line in lines[lines.index(HEADING) + 1 :]:
        if line.startswith("## "):
            break
        if not line.startswith("- "):
            continue
        match = _ITEM.match(line.rstrip())
        if match is None:
            problems.append(f"{GOAL_FILE}: {line.strip()[:80]!r} does not read '- <name> <outcome>', the name such "
                            "as G-build-sop, with [x] after the dash once it is done")  # fmt: skip
            continue
        done = match.group(1) in ("x", "X")
        rank = None if done else sum(1 for i in items if not i.done) + 1
        items.append(GoalItem(match.group(2), match.group(3), done, rank))
    names = [i.id for i in items]
    problems += [f"{GOAL_FILE} names {n} twice: each item's name is its own" for n in sorted(set(names))
                 if names.count(n) > 1]  # fmt: skip
    if not items and not problems:
        problems.append(f"{GOAL_FILE} lists no goal item under {HEADING}")
    if problems:
        raise GoalError(problems)
    return items


def ranks(items: list[GoalItem]) -> dict[str, int]:
    """Each open item's rank by its name."""
    return {i.id: i.rank for i in items if i.rank is not None}


def read(repo: Path, limit: int) -> list[GoalItem]:
    try:
        raw = files.read_repo_file(repo, GOAL_FILE, limit)
    except FileNotFoundError as exc:
        raise GoalError([f"{repo} has no {GOAL_FILE}"]) from exc
    return parse(raw.decode("utf-8", errors="strict"))
