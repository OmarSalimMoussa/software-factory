"""The goal items of the product's GOAL.md, in the order the owner ranks them (factory spec, "GOAL.md").

GOAL.md is the owner's file in a checkout another user owns, so it is read without following a symlink.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from runtime.queue import files

GOAL_FILE = "GOAL.md"
HEADING = "## The goal"
ITEM = re.compile(r"^- (?:\[[ xX]\] )?(G[1-9]\d*) (\S.*)$")


class GoalError(ValueError):
    """GOAL.md is missing its goal list, or its items are out of order."""


@dataclass(frozen=True, slots=True)
class GoalItem:
    id: str
    text: str


def parse(text: str) -> list[GoalItem]:
    lines = text.split("\n")
    if HEADING not in lines:
        raise GoalError(f"{GOAL_FILE} has no {HEADING} heading")
    items: list[GoalItem] = []
    for line in lines[lines.index(HEADING) + 1 :]:
        if line.startswith("## "):
            break
        match = ITEM.match(line.strip())
        if match:
            items.append(GoalItem(match.group(1), match.group(2)))
    expected = [f"G{n}" for n in range(1, len(items) + 1)]
    if [i.id for i in items] != expected:
        raise GoalError(f"{GOAL_FILE}'s goal items must run G1, G2, … in order")
    if not items:
        raise GoalError(f"{GOAL_FILE} lists no goal item under {HEADING}")
    return items


def read(repo: Path, limit: int) -> list[GoalItem]:
    try:
        raw = files.read_repo_file(repo, GOAL_FILE, limit)
    except FileNotFoundError as exc:
        raise GoalError(f"{repo} has no {GOAL_FILE}") from exc
    return parse(raw.decode("utf-8", errors="strict"))
