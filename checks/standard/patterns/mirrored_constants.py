"""Mirrored constants: each group's hand-kept copies hold the same value.

A value that cannot be imported everywhere it is needed (a compose file, a Dockerfile, a vendored file, a browser
table) is copied by hand, and a group makes the copies comparable. Each side is a file and a pattern whose ``expr``
group captures the value, matching exactly once (a ``//`` comment after a line-anchored pattern is tolerated). An
``int`` group evaluates integer arithmetic only, a ``str`` group compares the captured text, and a ``set`` group
compares the quoted strings captured, or with ``items = "keys"`` the object keys.
"""

from __future__ import annotations

import ast
import operator
import re
from collections.abc import Callable
from typing import Any

from checks.runner.repo import Repo
from checks.runner.values import PATTERN

ID = "mirrored-constants"
RULE = "Each group's copies hold the same value"
VALUES_FILE = "mirrored-constants.toml"
SIDE = {"path": str, "pattern": PATTERN, "items": str}
VALUES = {"groups": [{"name": str, "kind": str, "casefold": bool, "sides": [SIDE]}]}
KINDS = ("int", "str", "set")
ITEMS = ("strings", "keys")
Value = int | str | frozenset[str]

_OPS: dict[type[ast.operator], Callable[[int, int], int]] = {
    ast.Mult: operator.mul,
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Pow: operator.pow,
}
_TRAILING_COMMENT_RE = re.compile(r"\s+//.*$")
_QUOTED_RE = re.compile(r"""(["'])(.*?)\1""")
_KEY_RE = re.compile(r"""(?:^|[{,])\s*(["']?)([A-Za-z_][\w-]*)\1\s*:""", re.M)


def _eval(node: ast.expr) -> int:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    raise ValueError(f"not integer arithmetic: {ast.unparse(node)}")


def evaluate(expr: str) -> int:
    return _eval(ast.parse(expr.strip(), mode="eval").body)


def comment_tolerant(pattern: str) -> str:
    """A line-anchored pattern that also matches when a ``//`` comment follows it."""
    if "//" in pattern or not pattern.endswith("$"):
        return pattern
    return pattern[:-1] + r"(?:\s*//.*)?$"


def read_value(repo: Repo, side: dict[str, Any], kind: str) -> Value:
    path = repo.root / side["path"]
    if not path.is_file():
        raise ValueError(f"{side['path']} does not exist")
    found = [m.group("expr") for m in re.finditer(comment_tolerant(side["pattern"]), repo.read(side["path"]), re.M)]
    if len(found) != 1:
        raise ValueError(f"{side['path']}: expected exactly 1 match for {side['pattern']!r}, found {len(found)}")
    if kind == "str":
        return found[0].strip()
    if kind == "set":
        if side["items"] == "keys":
            return frozenset(m[1] for m in _KEY_RE.findall(found[0]))
        return frozenset(m[1] for m in _QUOTED_RE.findall(found[0]))
    return evaluate(_TRAILING_COMMENT_RE.sub("", found[0]))


def _shown(value: Value) -> str:
    return str(sorted(value)) if isinstance(value, frozenset) else str(value)


def _shape_problems(group: dict[str, Any]) -> list[str]:
    name = group["name"]
    out = [] if group["kind"] in KINDS else [f"{ID} ({name}): kind must be one of {', '.join(KINDS)}"]
    if len(group["sides"]) < 2:
        out.append(f"{ID} ({name}): a group needs at least two sides")
    for side in group["sides"]:
        if "expr" not in re.compile(side["pattern"]).groupindex:
            out.append(f"{ID} ({name}): {side['path']}'s pattern needs an (?P<expr>...) group")
        if side["items"] not in ITEMS:
            out.append(f"{ID} ({name}): {side['path']}'s items must be one of {', '.join(ITEMS)}")
    return out


def check_group(repo: Repo, group: dict[str, Any]) -> list[str]:
    problems = _shape_problems(group)
    if problems:
        return problems
    readings: list[tuple[str, Value]] = []
    for side in group["sides"]:
        try:
            readings.append((side["path"], read_value(repo, side, group["kind"])))
        # telemetry: surfaced — a side whose value cannot be read is one of the group's `problems`
        except (ValueError, SyntaxError) as exc:
            problems.append(f"{ID} ({group['name']}): {exc}")
    if problems:
        return problems

    def comparable(value: Value) -> Value:
        return value.casefold() if group["casefold"] and isinstance(value, str) else value

    first_path, first = readings[0]
    return [
        f"{ID} ({group['name']}): {path} has {_shown(value)}, {first_path} has {_shown(first)}"
        for path, value in readings[1:]
        if comparable(value) != comparable(first)
    ]


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    names = [group["name"] for group in values["groups"]]
    twice = sorted({name for name in names if names.count(name) > 1})
    problems = [f"{ID}: group name {name!r} is used twice" for name in twice]
    return problems + [p for group in values["groups"] for p in check_group(repo, group)]
