"""settings.toml's text with one value set, as the installer fills in what only it knows."""

from __future__ import annotations

import re
from typing import Any

from runtime.shared import settings as settings_module


def _value_end(line: str, start: int) -> int:
    """Where a TOML value ends on its line: at the first # outside a string."""
    quote, i = "", start
    while i < len(line):
        char = line[i]
        if quote:
            if char == "\\" and quote == '"':
                i += 1
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "#":
            return i
        i += 1
    return len(line)


def set_line(text: str, key: str, value: Any) -> str:
    """``text`` with the one line holding ``key`` set to ``value``, keeping the line's comment where it was."""
    group, _, name = key.partition(".")
    section = ""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and "]" in stripped:
            section = stripped[1 : stripped.index("]")].strip()
            continue
        match = re.match(rf"^\s*{re.escape(name)}\s*=\s*", line)
        if section != group or match is None:
            continue
        end = _value_end(line, match.end())
        new = settings_module.toml_value(value)
        comment = line[end:]
        if comment:
            new = new.ljust(max(end - match.end(), len(new) + 1))
        lines[index] = line[: match.end()] + new + comment
        return "\n".join(lines)
    raise KeyError(f"settings.toml has no line for {key}")
