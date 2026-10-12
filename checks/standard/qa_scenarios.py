"""QA scenarios: every file in qa/scenarios/ has its three labels and at most ``qa.max_steps`` steps.

The format is runtime/qa/scenarios.py's, the one reader the replay uses too; the most steps a scenario may have is
the factory's own setting, read from its settings.toml, since a scenario is one item of a tester's brief.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from checks.runner.repo import Repo
from runtime.qa import scenarios
from runtime.shared import settings as settings_mod

ID = "qa-scenarios"
RULE = "Scenarios have their three labels and at most qa.max_steps steps"
VALUES_FILE = None
VALUES = None

FACTORY_SETTINGS = Path(__file__).resolve().parents[2] / "settings.toml"


def max_steps() -> int:
    tree = tomllib.loads(FACTORY_SETTINGS.read_text(encoding="utf-8"))
    return int(settings_mod.read_some(tree, ("qa.max_steps",))["qa.max_steps"])


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    most = max_steps()
    found: list[str] = []
    for rel in sorted(r for r in repo.tracked if r.startswith(f"{scenarios.FOLDER}/") and r.endswith(".md")):
        if rel.count("/") != scenarios.FOLDER.count("/") + 1 or rel.endswith("/README.md"):
            continue
        meta = scenarios.front(repo.read(rel)) or {}
        protected = {p for p in meta.get("protects", "").split() if (repo.root / p).exists()}
        found += scenarios.problems(rel, repo.read(rel), protected, most)
    return found
