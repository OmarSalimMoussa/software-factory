"""Size ceilings: source 400 lines, tests 700, READMEs 100, other living docs 200, and 25 source files a folder.

Blank lines count: the number a reader scrolls is the number that matters. A unit over its ceiling fails unless
check-values/baseline.json pins it with a limit and its grounds. Folders that grow by design (a testdata corpus, icon
assets, database migration versions, dated specs) are exempt from the folder count.
"""

from __future__ import annotations

from collections import Counter
from pathlib import PurePosixPath
from typing import Any

from checks.runner import budgets, scope
from checks.runner.repo import Repo
from checks.runner.values import CEILING_STANZA

ID = "size-ceilings"
RULE = "Source 400 lines, tests 700, READMEs 100, docs 200, 25 files a folder, unless pinned with grounds"
VALUES_FILE = None
VALUES = None

CEILINGS = {"source-size": 400, "test-size": 700, "readme-size": 100, "doc-size": 200, "dir-files": 25}
REMEDIES = {
    "source-size": "split it by concern into a subfolder",
    "test-size": "split it by concern into a subfolder",
    "readme-size": "a README says what the part is and how it fits: move detail into a code comment at the "
    "constraint, or into docs/",
    "doc-size": "prune it, split real concerns apart, or pin it with recorded grounds",
    "dir-files": "split the folder by concern into subfolders",
}
_DIR_EXEMPT = frozenset({"testdata", "icons"})


def _lines(repo: Repo, rel: str) -> int:
    return len(repo.read(rel).splitlines())


def _counted_folder(rel: str) -> str | None:
    parts = rel.split("/")[:-1]
    migrations = any(a == "migrations" and b == "versions" for a, b in zip(parts, parts[1:], strict=False))
    if _DIR_EXEMPT & set(parts) or migrations:
        return None
    return "/".join(parts) or "."


def measure(repo: Repo) -> dict[str, dict[str, int]]:
    gates: dict[str, dict[str, int]] = {gate: {} for gate in CEILINGS}
    folders: Counter[str] = Counter()
    for rel in repo.tracked:
        kind = None if repo.excluded(rel) else scope.classify(rel)
        if kind == "readme":
            gates["readme-size"][rel] = _lines(repo, rel)
        elif kind == "tests":
            gates["test-size"][rel] = _lines(repo, rel)
        elif kind == "source":
            gates["source-size"][rel] = _lines(repo, rel)
            folder = _counted_folder(rel)
            if folder is not None:
                folders[folder] += 1
    gates["dir-files"] = dict(folders)
    for rel in scope.living_docs(repo.tracked):
        if PurePosixPath(rel).name != "README.md":
            gates["doc-size"][rel] = _lines(repo, rel)
    return gates


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    stanzas = {gate: repo.stanza(gate, CEILING_STANZA) for gate in CEILINGS}
    out: list[str] = []
    for gate, measured in measure(repo).items():
        out += budgets.check_ceiling(
            gate, CEILINGS[gate], measured, stanzas[gate], repo.head_stanza(gate), REMEDIES[gate]
        )
    return out
