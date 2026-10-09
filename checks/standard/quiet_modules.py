"""Quiet modules: the listed modules never log above info.

A module that handles workflow failures (what the product's users did wrong, not what broke) shows them where the user
sees them; logging them above info would send them to the error tracker as faults. Each listed module carries the
grounds for its silence. The patterns that make a log call loud are values, so any logging library fits.
"""

from __future__ import annotations

from typing import Any

from checks.runner.repo import Repo
from checks.runner.values import PATTERN
from checks.standard.patterns import engine

ID = "quiet-modules"
RULE = "Listed modules never log above info"
VALUES_FILE = "quiet-modules.toml"
VALUES = {"modules": {str: str}, "loud": [PATTERN], "remedy": str}


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    modules = tuple(sorted(values["modules"]))
    problems = [f"{ID}: {rel} is a test or test data; list only product modules" for rel in modules
                if engine.out_of_scope(rel)]  # fmt: skip
    rule = engine.Rule(
        label=ID,
        scans=modules,
        suffixes=(),
        rows=tuple((loud,) for loud in values["loud"]),
        message="logs above info in a quiet module",
        remedy=values["remedy"],
    )
    return problems + engine.scan(repo, rule)
