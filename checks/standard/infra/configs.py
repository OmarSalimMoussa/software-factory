"""Reading the config files the infrastructure checks compare: TOML, JSON and the YAML subset, and compose services."""

from __future__ import annotations

import json
import tomllib
from typing import Any

from checks.runner.repo import Repo
from checks.standard.infra import yaml_subset

FORMATS = ("toml", "json", "yaml")


class ConfigError(Exception):
    """A listed config file is missing or cannot be read; the message names it."""


def read(repo: Repo, rel: str, fmt: str) -> Any:
    path = repo.root / rel
    if not path.is_file():
        raise ConfigError(f"{rel} is missing")
    if fmt not in FORMATS:
        raise ConfigError(f"{rel}: format {fmt!r} is not one of {', '.join(FORMATS)}")
    try:
        text = path.read_text(encoding="utf-8")
        if fmt == "toml":
            return tomllib.loads(text)
        return json.loads(text) if fmt == "json" else yaml_subset.load(text)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ConfigError(f"{rel} cannot be read as {fmt}: {exc}") from exc


def services(repo: Repo, compose: str) -> dict[str, dict[str, Any]]:
    """The compose file's services by name, anchors and merge keys resolved."""
    loaded = read(repo, compose, "yaml")
    found = loaded.get("services") if isinstance(loaded, dict) else None
    if not isinstance(found, dict) or not all(isinstance(entry, dict) for entry in found.values()):
        raise ConfigError(f"{compose}: no services mapping")
    return found


def text_list(value: Any) -> list[str]:
    """A compose field that takes one string or a list of them (``env_file``), as a list."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(item["path"]) if isinstance(item, dict) else str(item) for item in items]
