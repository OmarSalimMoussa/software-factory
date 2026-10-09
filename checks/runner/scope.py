"""Which tracked files a check reads, by the standard's own classes (the same for every product).

Vendored and generated trees are never the product's own words. Approved specs under ``docs/specs/`` are dated
records: they quote the tree as it was, so the living-document checks skip them. A ``testdata/`` folder is a corpus.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

VENDOR_DIRS = frozenset({"vendor", "node_modules", "dist"})
DATED_RECORDS = ("docs/specs/",)
SOURCE_SUFFIXES = (".py", ".ts", ".tsx", ".mjs", ".sh", ".js", ".rs")
TS_SUFFIXES = (".ts", ".tsx", ".js", ".mjs")
COMMENT_SUFFIXES = (".py", *TS_SUFFIXES)
# `_fixtures.mjs` is test support like `conftest.py`: it registers no tests, so it takes the test ceiling.
_TEST_RE = re.compile(
    r"(^conftest\.py$|^test_.*\.py$|^_fixtures\.(mjs|js)$|_test\.(py|mjs|js|rs)$|\.(test|spec)\.(ts|tsx|js)$)"
)
PY_TEST_RE = re.compile(r"(_test\.py|conftest\.py|_testing\.py|_fakes\.py|(^|/)test_[^/]*\.py)$")


def vendored(rel: str) -> bool:
    return bool(VENDOR_DIRS & set(rel.split("/")[:-1]))


def in_testdata(rel: str) -> bool:
    return "testdata" in rel.split("/")[:-1]


def under(rel: str, prefixes: Iterable[str]) -> bool:
    """Whether ``rel`` is one of ``prefixes`` or lies under one (a prefix ending in / names a folder)."""
    for prefix in prefixes:
        folder = prefix.rstrip("/")
        if rel == folder or rel.startswith(f"{folder}/"):
            return True
    return False


def classify(rel: str) -> str | None:
    """``readme``, ``source`` or ``tests`` for a file the size and comment budgets measure, else None."""
    if rel.startswith(DATED_RECORDS) or vendored(rel):
        return None
    parts = rel.split("/")
    if parts[-1] == "README.md":
        return "readme"
    if not rel.endswith(SOURCE_SUFFIXES):
        return None
    if rel.endswith(".rs") and "tests" in parts[:-1]:
        # Cargo's convention: any .rs file under a `tests/` folder is a test, whatever it is named.
        return "tests"
    return "tests" if _TEST_RE.search(parts[-1]) else "source"


def living_docs(tracked: Iterable[str], excluded: Iterable[str] = ()) -> list[str]:
    """Every tracked Markdown file that describes the tree as it is now."""
    skip = (*DATED_RECORDS, *excluded)
    return [rel for rel in tracked if rel.endswith(".md") and not under(rel, skip) and not vendored(rel)]
