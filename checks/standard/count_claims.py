"""Count claims: living docs never claim "all <number>" or "the last <number>".

A count written in prose goes stale the day one more is added. "one" is not counted (neither phrase is idiomatic with
it) and "both" stays legal. Fenced code is skipped. A per-file ratchet against the default branch, where a new file
starts at zero.
"""

from __future__ import annotations

import re
from typing import Any

from checks.runner import scope
from checks.runner.repo import Repo

ID = "count-claims"
RULE = 'Docs never claim "all N"'
VALUES_FILE = None
VALUES = None

_NUMBERS = "two three four five six seven eight nine ten eleven twelve".split()
_CLAIM_RE = re.compile(
    rf"\ball (?:{'|'.join(_NUMBERS)}|\d+)\b|\bthe last (?:{'|'.join(_NUMBERS)}|\d+)\b", re.IGNORECASE
)
FENCE_RE = re.compile(r"^(?:```|~~~).*?^(?:```|~~~)[ \t]*$", re.M | re.S)
REMEDY = (
    'drop the number where it adds nothing ("all seven services" becomes "every service"), or word it so a test '
    "pins the count and say so"
)


def claims(text: str) -> list[tuple[int, str, str]]:
    stripped = FENCE_RE.sub("", text)
    return [
        (n, m.group(0), line.strip())
        for n, line in enumerate(stripped.splitlines(), 1)
        for m in _CLAIM_RE.finditer(line)
    ]


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    if repo.changed is None:
        return []  # a ratchet compares a branch's change with the default branch
    problems: list[str] = []
    for rel in scope.living_docs(repo.tracked):
        if rel not in repo.changed:
            continue
        found, old = claims(repo.read(rel)), repo.before(rel)
        was = len(claims(old)) if old is not None else 0
        if len(found) > was:
            shown = "\n".join(f"    {rel}:{n}: {claim!r} in {line}" for n, claim, line in found)
            problems.append(f"{ID}: {rel} has {len(found)}, over the {was} it has on the default branch; {REMEDY}\n"
                            f"{shown}")  # fmt: skip
    return problems
