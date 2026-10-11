"""Computation owners: each listed computation happens only in its owners.

A computation done twice drifts: a second clock origin, a second rounding rule, a value the browser derives that the
server already sends. Each computation names the folders it binds and how a line that performs it reads: a line
matches when every pattern of one ``when`` row matches it. Its owners carry the grounds for doing it, and an owner that
no longer performs it is stale.
"""

from __future__ import annotations

from typing import Any

from checks.runner.repo import Repo
from checks.runner.values import PATTERN
from checks.standard.patterns import engine

ID = "computation-owners"
RULE = "Listed computations happen only in their owners"
VALUES_FILE = "computation-owners.toml"
VALUES = {
    "computations": [
        {
            "name": str,
            "scans": [str],
            "suffixes": [str],
            "when": [[PATTERN]],
            "owners": {str: str},
            "code_only": bool,
            "remedy": str,
        }
    ]
}


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for computation in values["computations"]:
        empty = [n for n, row in enumerate(computation["when"]) if not row]
        if empty or not computation["when"]:
            problems.append(f"{ID} ({computation['name']}): every when row needs at least one pattern")
            continue
        rule = engine.Rule(
            label=f"{ID} ({computation['name']})",
            scans=tuple(computation["scans"]),
            suffixes=tuple(computation["suffixes"]),
            rows=tuple(tuple(row) for row in computation["when"]),
            message=f"computes {computation['name']} outside its owners",
            remedy=computation["remedy"],
            code_only=computation["code_only"],
            allow=dict(computation["owners"]),
        )
        problems += engine.scan(repo, rule)
    return problems
