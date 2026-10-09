"""Testing imports: only tests import test helpers.

A module named ``_testing`` or ``_fakes`` holds test builders and doubles. A product module that imports one runs a
test double in the product. Only ``*_test.py``, ``test_*.py``, ``conftest.py``, ``_testing.py`` and ``_fakes.py``
files may import a module with such a part in its dotted name. A ``testdata/`` fixture is skipped; any other Python
file that does not parse fails.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from typing import Any

from checks.runner import scope
from checks.runner.repo import Repo

ID = "testing-imports"
RULE = "Only tests import test helpers"
VALUES_FILE = None
VALUES = None

SCAFFOLDING = frozenset({"_testing", "_fakes"})
REMEDY = (
    "test helpers are for tests: move what the product needs into the module that owns it, or name this file "
    "*_test.py, test_*.py, conftest.py, _testing.py or _fakes.py if it is itself a test's"
)


def may_import(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name.endswith("_test.py") or name.startswith("test_") or name in {"conftest.py", "_testing.py", "_fakes.py"}


def _scaffolding(dotted: str) -> bool:
    return bool(SCAFFOLDING & set(dotted.split(".")))


def scaffolding_imports(source: str) -> Iterator[tuple[int, str]]:
    """Every (line, module) ``source`` imports that is a test helper; ``from pkg import _testing`` counts."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names if _scaffolding(alias.name))
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            if _scaffolding(node.module or ""):
                yield node.lineno, base
                continue
            for alias in node.names:
                if alias.name in SCAFFOLDING:
                    yield node.lineno, f"{base}.{alias.name}" if node.module else base + alias.name


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for rel in repo.tracked:
        if not rel.endswith(".py") or may_import(rel) or scope.in_testdata(rel) or scope.vendored(rel):
            continue
        try:
            found = list(scaffolding_imports(repo.read(rel)))
        except SyntaxError as exc:
            problems.append(f"{rel}:{exc.lineno}: does not parse as Python: {exc.msg}")
            continue
        problems += [f"{rel}:{line}: imports the test helper `{module}`; {REMEDY}" for line, module in found]
    return problems
