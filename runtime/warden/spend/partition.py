"""Where an agent's time went, from its transcript: each gap between two timed lines goes to one part, so the parts
add up to the transcript's span and nothing counts twice.

A gap belongs to the tools while a tool call is open, shared evenly by the open tools' keys; the Agent tool's share is
the agent waiting on its own subagents. Otherwise it is the model's after a user line or between two lines of one
reply, and idle after anything else: the agent's turn ended and it waits to be woken. What a pass leaves open waits in
the transcript's cursor for the next.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

WAITING = "Agent"
_USER = "user"
# A key keeps no more of a command than a `factory` command's verb words, or a command's own name.
_FACTORY = re.compile(r"^(?:cd\s+[^&;|]+&&\s*)?(factory(?:\s+-{0,2}[a-z][a-z-]*){1,2})")
_NAME = re.compile(r"^(?:cd\s+[^&;|]+&&\s*)?(?:[\w.-]*/)*([A-Za-z_][\w.-]*)(?:\s|$)")


def fresh(whole: bool) -> dict[str, Any]:
    """A transcript's state between passes: its latest moment, what its last line was, and its open tool calls."""
    return {"at": None, "after": "", "open": {}, "whole": whole}


def tool_key(name: str, given: object) -> str:
    if name != "Bash":
        return name.rsplit("__", 1)[-1]
    command = given.get("command") if isinstance(given, dict) else None
    text = command.strip() if isinstance(command, str) else ""
    found = _FACTORY.match(text) or _NAME.match(text)
    return f"Bash {found.group(1)}" if found else "Bash"


class Tally:
    """The parts one pass measured in one transcript."""

    def __init__(self) -> None:
        self.model = self.waiting = self.idle = 0.0
        self.tools: dict[str, float] = {}
        self.first: float | None = None
        self.last: float | None = None

    def tool(self, key: str, seconds: float) -> None:
        if key == WAITING:
            self.waiting += seconds
        else:
            self.tools[key] = self.tools.get(key, 0.0) + seconds


def _moment(entry: dict[str, Any]) -> float | None:
    try:
        return datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00")).timestamp()
    # telemetry: expected — a line with no readable time marks no moment; the gap runs on to the next that does
    except (KeyError, ValueError):
        return None


def step(state: dict[str, Any], entry: dict[str, Any], tally: Tally) -> None:
    """Count the gap up to ``entry`` and carry its line into ``state``."""
    at = _moment(entry)
    if at is None:
        return
    raw = entry.get("message")
    message = raw if isinstance(raw, dict) else {}
    reply = f"reply {message.get('id')}" if entry.get("type") == "assistant" else None
    if state["at"] is not None and at > state["at"]:
        _count(state, at - state["at"], reply, tally)
        tally.first = state["at"] if tally.first is None else tally.first
        tally.last = at
    state["at"] = at if state["at"] is None else max(state["at"], at)
    content = message.get("content")
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    if reply is not None:
        state["after"] = reply
        state["open"].update({str(b.get("id")): tool_key(str(b.get("name")), b.get("input"))
                              for b in blocks if b.get("type") == "tool_use"})  # fmt: skip
    elif entry.get("type") == _USER:
        state["after"] = _USER
        for b in blocks:
            if b.get("type") == "tool_result":
                state["open"].pop(str(b.get("tool_use_id")), None)


def _count(state: dict[str, Any], gap: float, reply: str | None, tally: Tally) -> None:
    keys = sorted(set(state["open"].values()))
    for key in keys:
        tally.tool(key, gap / len(keys))
    if keys:
        return
    if state["after"] in (_USER, reply):
        tally.model += gap
    else:
        tally.idle += gap
