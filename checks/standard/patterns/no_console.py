"""No console: browser code logs through its log module, never a bare ``console.<method>(``.

A product's log module sends a message to the console and to its error report; a bare console call reaches the
console alone, so the failure is invisible to telemetry. Comments and strings are masked first, so a note that names
a console call is no call. Named residual: a call inside a template literal's ``${...}`` reads as string text.
"""

from __future__ import annotations

from typing import Any

from checks.runner.repo import Repo
from checks.standard.patterns import engine

ID = "no-console"
RULE = "Browser code logs through its log module"
VALUES_FILE = "no-console.toml"
VALUES = {"roots": [str], "suffixes": [str], "log_modules": [str], "remedy": str}
CALL = r"\bconsole\.(log|warn|error|info|debug|trace)\("


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    rule = engine.Rule(
        label=ID,
        scans=tuple(values["roots"]),
        suffixes=tuple(values["suffixes"]),
        rows=((CALL,),),
        message="calls the console directly",
        remedy=values["remedy"],
        owners=tuple(values["log_modules"]),
    )
    return engine.scan(repo, rule)
