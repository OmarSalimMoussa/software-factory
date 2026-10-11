"""Reads the tables and fields of one parsed TOML file, collecting every problem rather than stopping at the
first: a missing or unknown key, a value of the wrong kind or out of range, a path outside the repo."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


class Fields:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def fail(self, where: str, what: str) -> None:
        self.problems.append(f"{where}: {what}")

    def table(
        self, data: dict[str, Any], key: str, where: str, keys: tuple[str, ...], optional: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        here = f"{where}.{key}" if where else f"[{key}]"
        value = data.get(key)
        if not isinstance(value, dict):
            self.fail(here, "missing" if value is None else "must be a table")
            return {}
        self.known(value, here, keys + optional)
        for name in keys:
            if name not in value:
                self.fail(f"{here}.{name}", "missing")
        return value

    def known(self, value: dict[str, Any], where: str, keys: tuple[str, ...]) -> None:
        for name in value:
            if name not in keys:
                self.fail(f"{where}.{name}", f"unknown key (allowed: {', '.join(keys)})")

    def text(
        self, t: dict[str, Any], key: str, where: str, pattern: re.Pattern[str] | None = None, what: str = ""
    ) -> str:
        value = t.get(key)
        if key not in t:
            return ""
        if not isinstance(value, str) or not value:
            self.fail(f"{where}.{key}", "must be a non-empty string")
            return ""
        if pattern is not None and not pattern.fullmatch(value):
            self.fail(f"{where}.{key}", f"{value!r} is not {what or 'well formed'}")
        return value

    def number(self, t: dict[str, Any], key: str, where: str, low: int, high: int) -> int:
        value = t.get(key)
        if key not in t:
            return 0
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            self.fail(f"{where}.{key}", f"must be a whole number from {low} to {high}")
            return 0
        return value

    def flag(self, t: dict[str, Any], key: str, where: str) -> bool:
        value = t.get(key)
        if key in t and not isinstance(value, bool):
            self.fail(f"{where}.{key}", "must be true or false")
        return value is True

    def texts(
        self, t: dict[str, Any], key: str, where: str, check: Any = None, empty_ok: bool = True
    ) -> tuple[str, ...]:
        value = t.get(key)
        if key not in t:
            return ()
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            self.fail(f"{where}.{key}", "must be a list of non-empty strings")
            return ()
        if not value and not empty_ok:
            self.fail(f"{where}.{key}", "must name at least one")
        if len(set(value)) != len(value):
            self.fail(f"{where}.{key}", "names something twice")
        if check is not None:
            for item in value:
                check(f"{where}.{key}", item)
        return tuple(value)

    def rel_path(self, where: str, value: str) -> None:
        parts = Path(value).parts
        if value.startswith(("/", "~")) or ".." in parts:
            self.fail(where, f"{value!r} must be a path inside the repo, relative to its top")

    def path_key(self, t: dict[str, Any], key: str, where: str, optional: bool = False) -> str:
        if optional and t.get(key, "") == "":
            if key in t and not isinstance(t[key], str):
                self.fail(f"{where}.{key}", "must be a string")
            return ""
        value = self.text(t, key, where)
        if value:
            self.rel_path(f"{where}.{key}", value)
        return value

    def items(self, t: dict[str, Any], key: str, where: str) -> list[tuple[str, dict[str, Any]]]:
        value = t.get(key)
        if key not in t:
            return []
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            self.fail(f"{where}.{key}", "must be a list of tables")
            return []
        return [(f"{where}.{key}[{n}]", item) for n, item in enumerate(value)]

    def _missing(self, t: dict[str, Any], key: str, where: str) -> str:
        if key not in t:
            self.fail(f"{where}.{key}", "missing")
        return ""

    def _missing_int(self, t: dict[str, Any], key: str, where: str) -> int:
        if key not in t:
            self.fail(f"{where}.{key}", "missing")
        return 0
