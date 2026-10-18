"""Every rule the guard refuses with is one the owner can see and switch off, and none is listed that never
refuses."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

from runtime.guard.rules import RULES

GUARD = Path(__file__).resolve().parents[2] / "runtime" / "guard"


def _refused_rules() -> set[str]:
    found = set()
    for source in GUARD.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "Refusal":
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                found.add(str(node.args[0].value))
            elif node.args and isinstance(node.args[0], ast.Attribute) and isinstance(node.args[0].value, ast.Name):
                # A rule named once for the guard and the warden, as `lanes.PLACES`: a module in runtime.shared.
                module = importlib.import_module(f"runtime.shared.{node.args[0].value.id}")
                found.add(str(getattr(module, node.args[0].attr)))
    return found


def test_every_refusing_rule_is_listed_for_its_switch() -> None:
    assert _refused_rules() == set(RULES)
