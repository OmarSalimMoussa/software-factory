"""History narrative: comments and docstrings of non-test source never narrate history.

Six phrases, case-insensitive, on a word boundary. Deliberately not a bare "used to": a purpose clause ("a flag used
to fetch the token") cannot be told from a history claim by a pattern, so only "used to be" counts. Tests may narrate
the bug they pin. A per-file ratchet that starts at zero, kept in check-values/baseline.json.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from checks.runner import budgets, comments, scope
from checks.runner.repo import Repo
from checks.runner.values import RATCHET_STANZA

ID = "history-narrative"
RULE = "Comments never narrate history"
VALUES_FILE = None
VALUES = None

PHRASE_RE = re.compile("|".join(rf"\b{re.escape(p)}\b" for p in budgets.NARRATION), re.IGNORECASE)
REMEDY = 'rephrase as a present-tense constraint, or delete pure history ("Deliberately no X: ..." for an absence)'


def hits(rel: str, text: str) -> list[tuple[int, str, str]]:
    lines = text.splitlines()
    found = comments.py_comment_lines(text) if rel.endswith(".py") else comments.ts_comment_lines(text)
    return [
        (n, m.group(0).lower(), lines[n - 1].strip()) for n in sorted(found) for m in PHRASE_RE.finditer(lines[n - 1])
    ]


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    counts: Counter[str] = Counter()
    found: dict[str, list[tuple[int, str, str]]] = {}
    problems: list[str] = []
    for rel in repo.tracked:
        if scope.classify(rel) != "source" or not rel.endswith(scope.COMMENT_SUFFIXES):
            continue
        try:
            found[rel] = hits(rel, repo.read(rel))
        except SyntaxError as exc:
            problems.append(f"{ID}: {rel} does not parse: {exc.msg}")
            continue
        counts[rel] = len(found[rel])
    units = repo.stanza(ID, RATCHET_STANZA)["units"]
    for rel, count in sorted(counts.items()):
        if count > units.get(rel, 0):
            shown = "\n".join(f"    {rel}:{n}: {phrase!r} in {text}" for n, phrase, text in found[rel])
            problems.append(f"{ID}: {rel} has {count}, over its baseline of {units.get(rel, 0)}; {REMEDY}\n{shown}")
    return problems
