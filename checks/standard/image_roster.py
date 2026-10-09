"""Image roster: every first-party image the code names is built by CI and deployed.

An image run by name works where a laptop builds it and fails where a deploy only pulls. For each roster, three lists
are held to one another: the images CI builds and the images a deploy pulls (each read from its file by a pattern
whose first group holds one or more names), and every ``<registry prefix><name>`` a tracked file names. A name ending
in ``-`` is a prefix a key is appended to at run time, covered when CI builds at least one image under it. An empty
build or pull list fails rather than passing vacuously.
"""

from __future__ import annotations

import re
from typing import Any

from checks.runner import scope
from checks.runner.repo import Repo
from checks.runner.values import PATTERN

ID = "image-roster"
RULE = "Every image the code names is built by CI and deployed"
VALUES_FILE = "image-roster.toml"
_LIST = {"file": str, "pattern": PATTERN}
VALUES = {"rosters": [{"registry": str, "built": _LIST, "pulled": _LIST, "skipped": [str], "skipped_suffixes": [str]}]}


def listed(text: str, pattern: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(pattern, text, re.M):
        for name in (match.group(1) or "").split():
            if name not in found:
                found.append(name)
    return found


def references(text: str, registry: str) -> list[str]:
    return list(dict.fromkeys(re.findall(re.escape(registry) + r"([a-z0-9][a-z0-9-]*)", text)))


def _covered(name: str, built: list[str]) -> bool:
    return any(one.startswith(name) for one in built) if name.endswith("-") else name in built


def _roster_problems(repo: Repo, roster: dict[str, Any]) -> list[str]:
    registry, lists = roster["registry"], {}
    for side in ("built", "pulled"):
        rel = roster[side]["file"]
        if not (repo.root / rel).is_file():
            return [f"{ID}: {rel} is missing"]
        lists[side] = listed(repo.read(rel), roster[side]["pattern"])
        if not lists[side]:
            return [f"{ID}: {rel} names no image its pattern reads; check the pattern in check-values"]
    built, pulled = lists["built"], lists["pulled"]
    ci, deploy = roster["built"]["file"], roster["pulled"]["file"]
    problems = [f"{ID}: CI builds {registry}{name} but a deploy never pulls it; add it to {deploy}"
                for name in built if name not in pulled]  # fmt: skip
    problems += [f"{ID}: a deploy pulls {registry}{name} but CI never builds it; add it to {ci}"
                 for name in pulled if name not in built]  # fmt: skip
    for rel in repo.tracked:
        if scope.under(rel, roster["skipped"]) or rel.endswith(tuple(roster["skipped_suffixes"])):
            continue
        try:
            text = (repo.root / rel).read_text(encoding="utf-8")
        except UnicodeDecodeError:  # a binary file names no image to run
            continue
        for name in references(text, registry):
            if not _covered(name, built):
                what = "an image under" if name.endswith("-") else "the image"
                problems.append(f"{ID}: {rel} runs {what} {registry}{name}, which CI never builds, so a deploy cannot "
                                f"pull it; add it to {ci}")  # fmt: skip
    return problems


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    return [problem for roster in values["rosters"] for problem in _roster_problems(repo, roster)]
