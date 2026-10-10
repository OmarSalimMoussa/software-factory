"""Server vocabularies copied, undeclared, into browser code, found by structure rather than by words.

A copy is a JS/TS collection of three or more strings (an array, a ``Set``, or an object's keys) holding exactly the
same strings as a collection in server Python (a tuple, list, set, frozenset, ``Literal[...]`` or a dict's keys). A file
whose first line carries a generated-file marker is no copy: its generator keeps it equal to the source.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from checks.runner import scope
from checks.runner.repo import Repo
from checks.standard.patterns import engine

MIN_ITEMS = 3
_EXCLUDED_DIRS = frozenset({"build"})
_JS_ARRAY_RE = re.compile(
    r"\b(?:const|let|var)\s+(\w+)\s*(?::[^=\n]+)?=\s*(?:Object\.freeze\(\s*)?(?:new\s+Set\(\s*)?\[([^\]]*)\]", re.S
)
_JS_OBJECT_RE = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*(?::[^=\n]+)?=\s*(?:Object\.freeze\(\s*)?\{([^{}]*)\}", re.S)
_JS_STRING_ITEM_RE = re.compile(r"""^\s*(["'])([^"'\\]*)\1\s*$""")
_JS_KEY_RE = re.compile(r"""(?:^|,)\s*(["']?)([A-Za-z_][\w-]*)\1\s*:""")


@dataclass(frozen=True)
class Found:
    web_path: str
    web_name: str
    web_line: int
    py_path: str
    py_name: str
    py_line: int


def _strings(nodes: Sequence[ast.expr | None]) -> list[str] | None:
    found = [n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return found if found and len(found) == len(nodes) else None


def _py_strings(node: ast.expr) -> list[str] | None:
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return _strings(node.elts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1:
        return _py_strings(node.args[0]) if node.func.id in {"frozenset", "set", "tuple"} else None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "Literal":
        return _strings(node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice])
    if isinstance(node, ast.Dict):
        return _strings(node.keys)
    return None


def python_collections(text: str) -> Iterable[tuple[str, int, frozenset[str]]]:
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        items_found = _py_strings(value) if isinstance(target, ast.Name) else None
        if items_found is not None and isinstance(target, ast.Name) and len(set(items_found)) >= MIN_ITEMS:
            yield target.id, node.lineno, frozenset(items_found)


def web_collections(text: str) -> Iterable[tuple[str, int, frozenset[str]]]:
    for match in _JS_ARRAY_RE.finditer(text):
        items = [item for item in match.group(2).split(",") if item.strip()]
        strings = [m.group(2) for m in map(_JS_STRING_ITEM_RE.match, items) if m]
        if strings and len(strings) == len(items) and len(set(strings)) >= MIN_ITEMS:
            yield match.group(1), text.count("\n", 0, match.start()) + 1, frozenset(strings)
    for match in _JS_OBJECT_RE.finditer(text):
        keys = [m[1] for m in _JS_KEY_RE.findall(match.group(2))]
        if len(set(keys)) >= MIN_ITEMS:
            yield match.group(1), text.count("\n", 0, match.start()) + 1, frozenset(keys)


def _in(rel: str, trees: Sequence[str], suffixes: tuple[str, ...]) -> bool:
    skipped = engine.out_of_scope(rel) or bool(_EXCLUDED_DIRS & set(rel.split("/")[:-1]))
    return rel.endswith(suffixes) and scope.under(rel, trees) and not skipped


def find(repo: Repo, server: Sequence[str], web: Sequence[str], markers: Sequence[str]) -> list[Found]:
    """Every web collection equal to a server collection; raises SyntaxError for server Python that does not parse."""
    python = [
        (rel, name, line, items_found)
        for rel in repo.tracked
        if _in(rel, server, (".py",)) and not repo.excluded(rel)
        for name, line, items_found in python_collections(repo.read(rel))
    ]
    found: list[Found] = []
    for rel in sorted(repo.tracked):
        if not _in(rel, web, scope.TS_SUFFIXES):
            continue
        text = repo.read(rel)
        if any(marker in text.split("\n", 1)[0] for marker in markers):
            continue
        for name, line, items_found in web_collections(text):
            found += [Found(rel, name, line, py_rel, py_name, py_line)
                      for py_rel, py_name, py_line, py_items in python if py_items == items_found]  # fmt: skip
    return found
