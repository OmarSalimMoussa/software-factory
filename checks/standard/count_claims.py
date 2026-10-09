"""Count claims: living docs never claim "all <number>" or "the last <number>".

A count written in prose goes stale the day one more is added. "one" is not counted (neither phrase is idiomatic with
it) and "both" stays legal. Fenced code is skipped. A per-file ratchet that starts at zero, kept in
check-values/baseline.json.
"""

from __future__ import annotations

import re
from typing import Any

from checks.runner import scope
from checks.runner.repo import Repo
from checks.runner.values import RATCHET_STANZA

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
    "pins the count and say so, then raise the file's baseline by hand"
)


def claims(text: str) -> list[tuple[int, str, str]]:
    stripped = FENCE_RE.sub("", text)
    return [
        (n, m.group(0), line.strip())
        for n, line in enumerate(stripped.splitlines(), 1)
        for m in _CLAIM_RE.finditer(line)
    ]


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    units = repo.stanza(ID, RATCHET_STANZA)["units"]
    problems: list[str] = []
    for rel in scope.living_docs(repo.tracked):
        found = claims(repo.read(rel))
        if len(found) > units.get(rel, 0):
            shown = "\n".join(f"    {rel}:{n}: {claim!r} in {line}" for n, claim, line in found)
            problems.append(
                f"{ID}: {rel} has {len(found)}, over its baseline of {units.get(rel, 0)}; {REMEDY}\n{shown}"
            )
    return problems
