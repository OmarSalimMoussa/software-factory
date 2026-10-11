"""A product's check values: one file per check in its check-values/, read against that check's schema.

A schema is plain data: a scalar type (``str``, ``int``, ``float``, ``bool``), ``PATTERN`` (a string that compiles as
a regular expression), ``[item]`` (a list of items), ``{str: item}`` (a table of any keys) or ``{"key": item, ...}``
(a table with exactly those keys, each required). Every problem in a file is listed at once; nothing is defaulted.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

VALUES_DIR = "check-values"
BASELINE = "baseline.json"


class Pattern:
    """A string value that must compile as a regular expression."""


PATTERN = Pattern()
Schema = Any  # recursive: a type, PATTERN, a one-item list, or a dict


class ValuesError(Exception):
    """A values file is missing, unreadable or does not fit its schema; the message lists every problem."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


def _kind(schema: Schema) -> str:
    if schema is PATTERN:
        return "a regular expression"
    if isinstance(schema, list):
        return "a list"
    if isinstance(schema, dict):
        return "a table"
    return {str: "a string", int: "a whole number", float: "a number", bool: "true or false"}[schema]


def problems(schema: Schema, value: Any, where: str) -> list[str]:
    """Why ``value`` does not fit ``schema``; empty when it does."""
    if schema is PATTERN:
        if not isinstance(value, str):
            return [f"{where} must be {_kind(schema)}"]
        try:
            re.compile(value)
        # telemetry: surfaced — a pattern that does not compile is one of the values file's `problems`
        except re.error as exc:
            return [f"{where} does not compile as a regular expression: {exc}"]
        return []
    if isinstance(schema, list):
        if not isinstance(value, list):
            return [f"{where} must be a list"]
        return [p for n, item in enumerate(value) for p in problems(schema[0], item, f"{where}[{n}]")]
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            return [f"{where} must be a table"]
        if set(schema) == {str}:
            return [p for key, item in value.items() for p in problems(schema[str], item, f"{where}.{key}")]
        out = [f"{where} is missing {key}" for key in schema if key not in value]
        out += [f"{where}.{key} is not a known value" for key in value if key not in schema]
        return out + [
            p for key, item in value.items() if key in schema for p in problems(schema[key], item, f"{where}.{key}")
        ]
    if schema is int or schema is float:
        ok = isinstance(value, int if schema is int else int | float) and not isinstance(value, bool)
        return [] if ok else [f"{where} must be {_kind(schema)}"]
    return [] if isinstance(value, schema) else [f"{where} must be {_kind(schema)}"]


def load(root: Path, name: str, schema: Schema) -> dict[str, Any]:
    """``check-values/<name>`` read and checked against ``schema``; raises ValuesError listing every problem."""
    rel = f"{VALUES_DIR}/{name}"
    path = root / rel
    if not path.is_file():
        raise ValuesError([f"{rel} is missing: every product supplies it (an empty list means nothing is listed)"])
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text) if name.endswith(".json") else tomllib.loads(text)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValuesError([f"{rel} cannot be read: {exc}"]) from exc
    found = problems(schema, data, rel)
    if found:
        raise ValuesError(found)
    assert isinstance(data, dict)
    return data


# The ceiling budgets several checks share: a stanza pins units over the standard ceiling with a
# limit and the grounds for it.
PIN = {"limit": int, "reason": str}
CEILING_STANZA = {"pins": {str: PIN}}
