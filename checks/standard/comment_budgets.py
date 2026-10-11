"""Comment budgets, from one comment counter (checks/runner/comments.py).

Three budgets: a module docblock holds at most 20 lines (a ceiling, pinned with grounds); a function or class
docstring over 20 lines is a per-file ratchet; each named tree's share of discretionary comment lines is a ratchet in
permille, and a new tree starts where the branch puts it, since it cannot start at zero. Both ratchets compare what
a branch changed with the default branch. The docblock leaves both
sides of the density ratio, and an error-telemetry marker leaves its numerator. A measured file outside every named
tree fails, so nothing drops out of the totals. A ``testdata/`` corpus is not measured.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from checks.runner import budgets, comments, scope
from checks.runner.repo import Repo
from checks.runner.values import CEILING_STANZA

ID = "comment-budgets"
RULE = "Short module docblocks; no growth in long docstrings or comment density"
VALUES_FILE = "comment-budgets.toml"
# trees: the folders density is measured over; a subtree listed before its tree is its own unit.
VALUES = {"trees": [str]}

DOCBLOCK_CEILING = 20
DOCSTRING_THRESHOLD = 20
DOCBLOCK_REMEDY = (
    "move genuine rationale to a code comment at the constraint, or to docs/, or prune restatement "
    "(a docblock cannot break behaviour, so only a readability pin is sound)"
)
_MARKER_RE = re.compile(r"(#|//)\s*(\w+:\s*)?telemetry:\s*(expected|surfaced|last-resort)\s+(—|--)\s")


def measured_file(rel: str) -> bool:
    if scope.in_testdata(rel) or rel == "conftest.py":
        return False
    return scope.classify(rel) in ("source", "tests") and rel.endswith(scope.COMMENT_SUFFIXES)


def tree_of(rel: str, trees: list[str]) -> str | None:
    return next((tree for tree in trees if rel.startswith(f"{tree.rstrip('/')}/")), None)


def file_counts(rel: str, text: str) -> tuple[int, int, int, int]:
    """(module docblock lines, docstrings over the threshold, discretionary comment lines, non-blank lines)."""
    docblock = comments.module_docblock_span(rel, text)
    lines = comments.py_comment_lines(text) if rel.endswith(".py") else comments.ts_comment_lines(text)
    markers = {n for n, line in enumerate(text.splitlines(), 1) if _MARKER_RE.search(line)}
    nonblank = {n for n, line in enumerate(text.splitlines(), 1) if line.strip()} - docblock
    return (
        len(docblock),
        comments.docstrings_over(rel, text, DOCSTRING_THRESHOLD),
        len(lines - docblock - markers),
        len(nonblank),
    )


def measure(repo: Repo, trees: list[str]) -> tuple[dict[str, dict[str, int]], list[str]]:
    docblock: dict[str, int] = {}
    docstrings: dict[str, int] = {}
    tree_comment: Counter[str] = Counter()
    tree_total: Counter[str] = Counter()
    outside: list[str] = []
    for rel in repo.tracked:
        if not measured_file(rel) or repo.excluded(rel):
            continue
        try:
            docblock[rel], docstrings[rel], discretionary, nonblank = file_counts(rel, repo.read(rel))
        # telemetry: surfaced — a file that does not parse is a problem in `outside`, which the check reports
        except SyntaxError as exc:
            outside.append(f"{ID}: {rel} does not parse: {exc.msg}")
            continue
        tree = tree_of(rel, trees)
        if tree is None:
            outside.append(f"tree-density: {rel} is outside every tree in check-values/{VALUES_FILE}: add its tree")
            continue
        tree_comment[tree] += discretionary
        tree_total[tree] += nonblank
    density = {tree: tree_comment[tree] * 1000 // total if total else 0 for tree, total in tree_total.items()}
    return {"module-docblock": docblock, "docstring-length": docstrings, "tree-density": density}, outside


def measure_before(
    repo: Repo, trees: list[str], docstrings: list[str], density: list[str]
) -> dict[str, dict[str, int]]:
    """The two ratchets as they stood on the default branch: ``docstrings``, each file's long docstrings, and
    ``density``, each tree's comment density over every file it held there."""
    found: dict[str, dict[str, int]] = {"docstring-length": {}, "tree-density": {}}
    for rel in docstrings:
        old = repo.before(rel)
        try:
            if old is not None:
                found["docstring-length"][rel] = file_counts(rel, old)[1]
        # telemetry: expected — a file that did not parse on the default branch starts at zero
        except SyntaxError:
            continue
    comment, total = Counter[str](), Counter[str]()
    for rel in repo.tracked_before():
        tree = tree_of(rel, trees) if measured_file(rel) else None
        old = repo.before(rel) if tree in density else None
        if tree is None or old is None:
            continue
        try:
            _, _, discretionary, nonblank = file_counts(rel, old)
        # telemetry: expected — a file that did not parse on the default branch adds nothing to its tree
        except SyntaxError:
            continue
        comment[tree] += discretionary
        total[tree] += nonblank
    found["tree-density"] = {tree: comment[tree] * 1000 // n if n else 0 for tree, n in total.items()}
    return found


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    gates, outside = measure(repo, values["trees"])
    out = budgets.check_ceiling(
        "module-docblock",
        DOCBLOCK_CEILING,
        gates["module-docblock"],
        repo.stanza("module-docblock", CEILING_STANZA),
        repo.head_stanza("module-docblock"),
        DOCBLOCK_REMEDY,
    )
    if repo.changed is None:
        return out + outside  # a ratchet compares a branch's change with the default branch
    files = sorted(rel for rel in gates["docstring-length"] if rel in repo.changed)
    trees = sorted({tree for rel in repo.changed if (tree := tree_of(rel, values["trees"])) is not None})
    before = measure_before(repo, values["trees"], files, trees)
    out += budgets.check_ratchet(
        "docstring-length", {rel: gates["docstring-length"][rel] for rel in files}, before["docstring-length"]
    )
    density = {tree: gates["tree-density"][tree] for tree in trees if tree in gates["tree-density"]}
    out += budgets.check_ratchet("tree-density", density, before["tree-density"], new_units=True)
    return out + outside
