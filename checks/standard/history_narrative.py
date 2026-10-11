"""History narrative: comments and docstrings of non-test source never narrate history.

Six phrases, case-insensitive, on a word boundary. Deliberately not a bare "used to": a purpose clause ("a flag used
to fetch the token") cannot be told from a history claim by a pattern, so only "used to be" counts. Tests may narrate
the bug they pin. A per-file ratchet against the default branch, where a new file starts at zero.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from checks.runner import budgets, comments, scope
from checks.runner.repo import Repo

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
        if scope.classify(rel) != "source" or not rel.endswith(scope.COMMENT_SUFFIXES) or repo.excluded(rel):
            continue
        try:
            found[rel] = hits(rel, repo.read(rel))
        # telemetry: surfaced — a file that does not parse is one of the check's `problems`
        except SyntaxError as exc:
            problems.append(f"{ID}: {rel} does not parse: {exc.msg}")
            continue
        counts[rel] = len(found[rel])
    if repo.changed is None:
        return problems  # a ratchet compares a branch's change with the default branch
    for rel, count in sorted(counts.items()):
        if rel not in repo.changed:
            continue
        was = _count_before(repo, rel)
        if count > was:
            shown = "\n".join(f"    {rel}:{n}: {phrase!r} in {text}" for n, phrase, text in found[rel])
            problems.append(f"{ID}: {rel} has {count}, over the {was} it has on the default branch; {REMEDY}\n{shown}")
    return problems


def _count_before(repo: Repo, rel: str) -> int:
    old = repo.before(rel)
    try:
        return len(hits(rel, old)) if old is not None else 0
    # telemetry: expected — a file that did not parse on the default branch: every hit is the branch's own
    except SyntaxError:
        return 0
