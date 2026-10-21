"""Facts the factory reads from Claude Code's own transcripts, never from what an agent says about itself.

A session's transcript is a JSON-lines file; each subagent's is
``<transcript without .jsonl>/subagents/agent-<id>.jsonl`` and opens with its brief. These readers give the tool
calls a session made, when each was answered, and a tester's brief; history.py keeps the subagents a session started
and the calls the guard refused.
"""

from __future__ import annotations

import bisect
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# A tester's brief declares its scenarios and checks on a line of its own; the time budget follows from it.
BUDGET_LINE = re.compile(r"^[ \t]*Scenarios and checks:[ \t]*(\S*)[ \t]*$", re.MULTILINE)
# A real brief is a few kilobytes; a longer first line is not one Claude Code wrote.
_FIRST_LINE_LIMIT = 1_000_000
_NEWLINE = re.compile(b"\n")


class BriefError(Exception):
    """A tester's brief or start time cannot be read; the message says why."""


# One decision asks a transcript several times, and a long session's runs to megabytes, slow to parse on a busy
# machine: the last one read stays read until it changes or the next decision begins (forget), each line parsed once.
_last: tuple[tuple[Path, int, int], bytes, list[int], dict[int, dict[str, Any] | None]] | None = None


def forget() -> None:
    global _last
    _last = None


def entries(path: Path, needles: tuple[str, ...] = ()) -> Iterator[dict[str, Any]]:
    """Each line of a transcript written whole, or only those whose JSON text holds one of ``needles``."""
    global _last
    seen = path.stat()
    key = (path, seen.st_size, seen.st_mtime_ns)
    if _last is None or _last[0] != key:
        raw = path.read_bytes()
        _last = (key, raw, line_starts(raw), {})
    _, raw, starts, read = _last
    for n in holding(raw, starts, needles) if needles else range(len(starts)):
        if n not in read:
            read[n] = parsed(raw[starts[n] : starts[n + 1] if n + 1 < len(starts) else len(raw)])
        entry = read[n]
        if entry is not None:
            yield entry


def line_starts(raw: bytes) -> list[int]:
    return [0, *(found.end() for found in _NEWLINE.finditer(raw))]


def holding(raw: bytes, starts: list[int], needles: tuple[str, ...]) -> list[int]:
    """The lines, by number, holding any of ``needles``, found in the whole text at once: JSON has no raw newline."""
    found: set[int] = set()
    for needle in (n.encode() for n in needles):
        at = raw.find(needle)
        while at >= 0:
            line = bisect.bisect_right(starts, at) - 1
            found.add(line)
            at = raw.find(needle, starts[line + 1]) if line + 1 < len(starts) else -1
    return sorted(found)


def json_text(value: str) -> str:
    """A string as Claude Code writes it into a line, as a needle: JSON-escaped, any other character as itself."""
    return json.dumps(value, ensure_ascii=False)


def parsed(line: bytes) -> dict[str, Any] | None:
    try:
        entry = json.loads(line.decode("utf-8", errors="replace"))
    # telemetry: expected — a line still being written is skipped until it is whole
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def entry_time(entry: dict[str, Any]) -> datetime | None:
    stamp = entry.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    # telemetry: expected — an entry with no readable time is skipped by every caller
    except ValueError:
        return None


def text_of(content: object) -> str:
    """The text of a message's content, whether a plain string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [str(block.get("text", "")) for block in content if isinstance(block, dict)]
        return "\n".join(parts)
    return ""


def blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def messaged_since_prompt(path: Path, to: str) -> bool:
    """Whether the session sent ``to`` a message (SendMessage) since its last prompt: whatever starts a turn, be it a
    person's words, another session's message or a background task's notice. A tool's result, and the skill text a
    Skill call loads (meta, with no peer behind it), arrive inside a turn and start none. A reply to the address a
    message from ``to`` came from counts as one to ``to``: Claude Code tells a session to answer that way."""
    sent, names = False, {to}
    for entry in entries(path):
        message = entry.get("message")
        role = message.get("role") if isinstance(message, dict) else None
        origin = entry.get("origin")
        if isinstance(origin, dict) and origin.get("kind") == "peer" and origin.get("name") == to:
            names.add(str(origin.get("from")))
        if role == "user" and _prompts(entry):
            sent = False
        elif role == "assistant":
            sent = sent or any(_message_to(block, names) for block in blocks(entry))
    return sent


def _prompts(entry: dict[str, Any]) -> bool:
    if any(block.get("type") == "tool_result" for block in blocks(entry)):
        return False
    origin = entry.get("origin")
    return not entry.get("isMeta") or (isinstance(origin, dict) and origin.get("kind") == "peer")


def _message_to(block: dict[str, Any], names: set[str]) -> bool:
    args = block.get("input")
    return (
        block.get("type") == "tool_use"
        and block.get("name") == "SendMessage"
        and isinstance(args, dict)
        and args.get("to") in names
    )


def subagent_transcript(session_transcript: Path, agent_id: str) -> Path:
    return session_transcript.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"


def declared_items(brief: str, max_items: int) -> int:
    """The scenarios and checks a tester's brief declares, from 1 to ``max_items``."""
    values = BUDGET_LINE.findall(brief)
    if len(values) != 1:
        found = f"{len(values)} lines" if values else "no line"
        raise BriefError(f'the brief has {found} "Scenarios and checks: N" and needs exactly one')
    value = values[0]
    if not value.isdigit() or not 1 <= int(value) <= max_items:
        raise BriefError(f'"Scenarios and checks: {value}" is not a whole number from 1 to {max_items}')
    return int(value)


def first_brief(transcript: Path) -> tuple[str, datetime]:
    """A subagent's brief and when it arrived: the first line of its own transcript, which it did not write."""
    try:
        with transcript.open(encoding="utf-8") as lines:
            entry = json.loads(lines.readline(_FIRST_LINE_LIMIT))
    except (OSError, ValueError) as exc:
        raise BriefError(f"cannot read the tester's transcript: {type(exc).__name__}") from exc
    when = entry_time(entry) if isinstance(entry, dict) else None
    message = entry.get("message") if isinstance(entry, dict) else None
    if when is None or not isinstance(message, dict):
        raise BriefError("the tester's transcript does not open with a dated brief")
    return text_of(message.get("content")), when


@dataclass(slots=True)
class ToolUse:
    """One tool call in a transcript."""

    tool_use_id: str
    when: datetime
    name: str
    args: dict[str, Any]


def used_any(path: Path, prefixes: tuple[str, ...]) -> bool:
    """Whether a transcript holds a call of any tool whose name starts with one of the prefixes."""
    return any(
        block.get("type") == "tool_use" and str(block.get("name", "")).startswith(prefixes)
        for entry in entries(path, tuple(json_text(prefix)[:-1] for prefix in prefixes))
        for block in blocks(entry)
    )


def answered(path: Path) -> dict[str, datetime]:
    """When each tool call in a transcript got its result, by the call's id."""
    found: dict[str, datetime] = {}
    for entry in entries(path, (json_text("tool_result"),)):
        when = entry_time(entry)
        for block in blocks(entry):
            if when is not None and block.get("type") == "tool_result":
                found[str(block.get("tool_use_id", ""))] = when
    return found


def tool_uses(path: Path, names: frozenset[str]) -> list[ToolUse]:
    """Every call of the named tools in a transcript, oldest first."""
    found: list[ToolUse] = []
    for entry in entries(path, tuple(json_text(name) for name in names)):
        when = entry_time(entry)
        if when is None:
            continue
        for block in blocks(entry):
            if block.get("type") == "tool_use" and block.get("name") in names:
                raw = block.get("input")
                found.append(
                    ToolUse(
                        str(block.get("id", "")),
                        when,
                        str(block["name"]),
                        raw if isinstance(raw, dict) else {},
                    )
                )
    return found
