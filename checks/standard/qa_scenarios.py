"""QA scenarios: every file in qa/scenarios/ has its three labels and at most ``qa.max_steps`` steps, and one a
branch adds or changes that signs in as a named address lists a ``person <role>``: the orchestrator hands testers
their people by ``needs``. Only changed ones, so a branch that left before older scenarios were fixed is not failed
on files it never touched.

The format is runtime/qa/scenarios.py's, the one reader the replay uses too; the most steps a scenario may have is
the factory's own setting, read from its settings.toml, since a scenario is one item of a tester's brief.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from checks.runner.repo import Repo
from runtime.qa import scenarios
from runtime.shared import settings as settings_mod

ID = "qa-scenarios"
RULE = "Scenarios have their three labels, at most qa.max_steps steps, and a person for an address they sign in as"
VALUES_FILE = None
VALUES = None

FACTORY_SETTINGS = Path(__file__).resolve().parents[2] / "settings.toml"
# The factory's sign-in verb with an address, across a line break; a placeholder such as `<address>` names none.
_SIGNS_IN = re.compile(r"factory\s+slot\s+sign-in\s+([^\s`<>]+@[^\s`<>]+)")


def max_steps() -> int:
    tree = tomllib.loads(FACTORY_SETTINGS.read_text(encoding="utf-8"))
    return int(settings_mod.read_some(tree, ("qa.max_steps",))["qa.max_steps"])


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    most = max_steps()
    found: list[str] = []
    for rel in sorted(r for r in repo.tracked if r.startswith(f"{scenarios.FOLDER}/") and r.endswith(".md")):
        if rel.count("/") != scenarios.FOLDER.count("/") + 1 or rel.endswith("/README.md"):
            continue
        text = repo.read(rel)
        meta = scenarios.front(text) or {}
        protected = {p for p in meta.get("protects", "").split() if (repo.root / p).exists()}
        found += scenarios.problems(rel, text, protected, most)
        if repo.changed is None or rel in repo.changed:
            found += _unnamed(rel, meta, text)
    return found


def _unnamed(rel: str, meta: dict[str, str], text: str) -> list[str]:
    addresses = sorted({found.rstrip(".,;:)") for found in _SIGNS_IN.findall(text)})
    if not addresses or any(need.strip().startswith("person ") for need in meta.get("needs", "").split(",")):
        return []
    return [f"{rel}: signs in as {', '.join(addresses)} but needs names no person; add the role that address plays"]
