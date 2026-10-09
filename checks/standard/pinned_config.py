"""Pinned config: listed keys in listed config files keep their listed values.

A config that departs from a tool's own defaults on purpose (a boundary, a strictness, a safety switch) can be undone
quietly by an upgrade that folds the upstream value back in. Each pin names the file, its format, the key's path, the
value it must hold and why. A missing file, a missing key and a value of another type fail as loudly as a wrong one.
"""

from __future__ import annotations

from typing import Any

from checks.runner.repo import Repo
from checks.standard.infra import configs

ID = "pinned-config"
RULE = "Listed keys in listed config files keep their listed values"
VALUES_FILE = "pinned-config.toml"
VALUES = {"pins": [{"file": str, "format": str, "path": [str], "value": object, "reason": str}]}


def _dig(doc: Any, path: list[str]) -> tuple[bool, Any]:
    node = doc
    for key in path:
        if isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        elif isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return False, None
    return True, node


def _same(got: Any, want: Any) -> bool:
    # A bool is an int in Python; a pinned `false` must not accept a 0.
    return type(got) is type(want) and got == want


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    docs: dict[tuple[str, str], Any] = {}
    for pin in values["pins"]:
        where = f"{pin['file']}: `{'.'.join(pin['path'])}`"
        key = (pin["file"], pin["format"])
        if key not in docs:
            try:
                docs[key] = configs.read(repo, pin["file"], pin["format"])
            except configs.ConfigError as exc:
                docs[key] = exc
            if isinstance(docs[key], configs.ConfigError):
                problems.append(f"{ID}: {docs[key]}")
        doc = docs[key]
        if isinstance(doc, configs.ConfigError):
            continue
        found, got = _dig(doc, pin["path"])
        if not found:
            problems.append(f"{ID}: {where} is absent, must be {pin['value']!r}: {pin['reason']}")
        elif not _same(got, pin["value"]):
            problems.append(f"{ID}: {where} is {got!r}, must be {pin['value']!r}: {pin['reason']}")
    return problems
