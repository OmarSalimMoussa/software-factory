"""Network door: outbound network calls go only through the product's named door modules.

Each door lists the calls that open a link (a bare client, a bare ``fetch(``, a second socket), the folders it binds and
the modules that own the link's retry policy and deadline. A call anywhere else is a link nothing at the call site
bounds. A line matching an ``unless`` pattern (a type position) is no call. Allowed files carry grounds and must answer
a live call, or they are stale.
"""

from __future__ import annotations

from typing import Any

from checks.runner.repo import Repo
from checks.runner.values import PATTERN
from checks.standard.patterns import engine

ID = "network-door"
RULE = "Outbound network calls go only through the named modules"
VALUES_FILE = "network-door.toml"
VALUES = {
    "doors": [
        {
            "name": str,
            "scans": [str],
            "suffixes": [str],
            "calls": [PATTERN],
            "unless": [PATTERN],
            "owners": [str],
            "allow": {str: str},
            "code_only": bool,
            "remedy": str,
        }
    ]
}


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for door in values["doors"]:
        rule = engine.Rule(
            label=f"{ID} ({door['name']})",
            scans=tuple(door["scans"]),
            suffixes=tuple(door["suffixes"]),
            rows=tuple((call,) for call in door["calls"]),
            message=f"opens its own link ({door['name']})",
            remedy=door["remedy"],
            code_only=door["code_only"],
            unless=tuple(door["unless"]),
            owners=tuple(door["owners"]),
            allow=dict(door["allow"]),
        )
        problems += engine.scan(repo, rule)
    return problems
