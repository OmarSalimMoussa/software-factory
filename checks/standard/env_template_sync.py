"""Env template sync: every key in an env template is in the local env file it is copied to (extra keys are fine).

Each pair names a tracked template and the local env file made from it, relative to the repo root. A missing template
or a missing env file fails and names it: wherever the check runs (a laptop, a worktree, CI), the env file must
exist there, made from the template the way the product makes it.
"""

from __future__ import annotations

import re
from typing import Any

from checks.runner.repo import Repo

ID = "env-template-sync"
RULE = "Every key in the env template is in the local env file"
VALUES_FILE = "env-template-sync.toml"
VALUES = {"pairs": [{"template": str, "env": str}]}

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")


def _keys(text: str) -> list[str]:
    return [m.group(1) for line in text.splitlines() if (m := _KEY_RE.match(line))]


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for pair in values["pairs"]:
        template, env = repo.root / pair["template"], repo.root / pair["env"]
        if not template.is_file():
            problems.append(f"{ID}: the template {pair['template']} is missing")
            continue
        if not env.is_file():
            problems.append(f"{ID}: {pair['env']} is missing: make it from {pair['template']} before checking")
            continue
        have = set(_keys(env.read_text(encoding="utf-8", errors="replace")))
        wanted = dict.fromkeys(_keys(template.read_text(encoding="utf-8")))
        problems += [
            f"{ID}: {pair['env']} lacks {key}, which {pair['template']} lists" for key in wanted if key not in have
        ]
    return problems
