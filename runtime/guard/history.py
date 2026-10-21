"""What a session did that the guard counts: the subagents it started, which of them finished, and the calls the guard
refused. Read from Claude Code's transcript of the session, never from what an agent says about itself.

A long session's transcript runs to tens of megabytes, and the guard asks this on every subagent start: read whole each
time, the asking outran the guard's time while the factory's slice was throttled. So what was read is kept beside the
transcript, in the session's own folder under ~/.claude, which no agent may write, and each decision reads only the
lines added since, a step at a time, keeping each step: a decision cut short still spares the next one that work. A
kept read whose last bytes are not the transcript's at that point is dropped, and the transcript read from its start.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from runtime.guard import transcript
from runtime.guard.wire import REFUSED
from runtime.shared import store

KEPT = "guard-history.json"
_STEP = 8_000_000
_TAIL = 4096
_AGENT_ID = re.compile(r"agentId:\s*([A-Za-z0-9_-]+)")
_NOTICE_ID = re.compile(r"<task-id>([A-Za-z0-9_-]+)</task-id>")
# A subagent's final report; a message it sends while it still runs carries no hand-back line.
_HANDBACK_ID = re.compile(r'<agent-message from="([A-Za-z0-9_-]+)">\s*\[Subagent hand-back\]')
_DONE = re.compile(r"<status>(completed|failed|killed|stopped|cancelled)</status>")
_REFUSALS = re.compile(re.escape(REFUSED) + r"(?: \([^)\s]*\))?")
_NEEDLES = (
    transcript.json_text("Agent"),
    transcript.json_text("tool_result"),
    "<task-notification>",
    "<agent-message ",
    transcript.json_text(REFUSED)[1:-1],
)


@dataclass(slots=True)
class AgentCall:
    """One Agent tool call a session made: which subagent, when, with what brief and model, and whether it is done. The
    model is the one the call asked for, empty when the subagent runs on its definition's own."""

    tool_use_id: str
    subagent_type: str
    started: datetime
    prompt: str
    model: str
    agent_id: str | None
    finished: bool


@dataclass(slots=True)
class _Read:
    at: int = 0
    tail: str = ""
    calls: dict[str, AgentCall] = field(default_factory=dict)
    done: set[str] = field(default_factory=set)
    refusals: dict[str, list[str]] = field(default_factory=dict)

    def add(self, entry: dict[str, Any]) -> None:
        when = transcript.entry_time(entry)
        for text in _delivered(entry):
            _note_done(text, self.done)
        for block in transcript.blocks(entry):
            kind = block.get("type")
            if kind == "tool_use" and block.get("name") == "Agent" and when is not None:
                call = _started(block, when)
                self.calls[call.tool_use_id] = call
            elif kind == "tool_result":
                use, text = str(block.get("tool_use_id", "")), transcript.text_of(block.get("content"))
                if REFUSED in text:
                    self.refusals[use] = _REFUSALS.findall(text)
                _resulted(self.calls.get(use), text)


_last: tuple[tuple[Path, int, int], _Read] | None = None


def forget() -> None:
    global _last
    _last = None


def agent_calls(path: Path) -> list[AgentCall]:
    """Every Agent call in a session's transcript, oldest first, each marked finished once its result arrived."""
    read = _read(path)
    calls = [dataclasses.replace(c, finished=c.finished or c.agent_id in read.done) for c in read.calls.values()]
    # A foreground subagent names no id until it reports; Claude Code's record beside its transcript names it at once.
    named = subagent_ids(path) if any(c.agent_id is None for c in calls) else {}
    for call in calls:
        if call.agent_id is None:
            call.agent_id = named.get(call.tool_use_id)
    return sorted(calls, key=lambda c: c.started)


def refused_ids(path: Path, marker: str) -> set[str]:
    """The tool calls whose result carries the marker: the calls a hook refused, which never ran."""
    return {use for use, found in _read(path).refusals.items() if any(marker in refusal for refusal in found)}


def subagent_ids(session_transcript: Path) -> dict[str, str]:
    """Each subagent's id by the Agent call that started it, from the record Claude Code keeps beside its transcript."""
    found: dict[str, str] = {}
    folder = session_transcript.with_suffix("") / "subagents"
    for meta in folder.glob("agent-*.meta.json"):
        try:
            use = json.loads(meta.read_text(encoding="utf-8")).get("toolUseId")
        # telemetry: expected — a record still being written names nobody yet
        except (OSError, ValueError):
            continue
        if isinstance(use, str):
            found[use] = meta.name.removeprefix("agent-").removesuffix(".meta.json")
    return found


def _read(path: Path) -> _Read:
    """The session's history up to the transcript's last whole line, saved after each step read."""
    global _last
    seen = path.stat()
    key = (path, seen.st_size, seen.st_mtime_ns)
    if _last is not None and _last[0] == key:
        return _last[1]
    kept = path.with_suffix("") / KEPT
    with path.open("rb") as fh:
        read = _kept(kept, fh, seen.st_size)
        while chunk := _whole_lines(fh, read.at):
            starts = transcript.line_starts(chunk)
            for n in transcript.holding(chunk, starts, _NEEDLES):
                entry = transcript.parsed(chunk[starts[n] : starts[n + 1]])
                if entry is not None:
                    read.add(entry)
            read.at += len(chunk)
            read.tail = _tail(fh, read.at)
            store.write_text(kept, json.dumps(_plain(read)), 0o600)
    _last = (key, read)
    return read


def _whole_lines(fh: IO[bytes], at: int) -> bytes:
    """The next step's whole lines from ``at``; a line longer than a step is read to its end."""
    fh.seek(at)
    chunk = fh.read(_STEP)
    while chunk and b"\n" not in chunk and (more := fh.read(_STEP)):
        chunk += more
    return chunk[: chunk.rfind(b"\n") + 1]


def _tail(fh: IO[bytes], at: int) -> str:
    fh.seek(max(0, at - _TAIL))
    return hashlib.sha256(fh.read(min(at, _TAIL))).hexdigest()


def _kept(kept: Path, fh: IO[bytes], size: int) -> _Read:
    """The read kept beside the transcript when the transcript still holds what it read; else a read from the start."""
    try:
        raw = store.read_json_or(kept, None)
        read = None if raw is None else _from_plain(raw)
    # telemetry: expected — a kept read this guard cannot load is redone from the transcript, which holds it all
    except (store.StoreError, KeyError, TypeError, ValueError, AttributeError):
        read = None
    if read is None or read.at > size or _tail(fh, read.at) != read.tail:
        return _Read()
    return read


def _plain(read: _Read) -> dict[str, Any]:
    calls = [{**dataclasses.asdict(c), "started": c.started.isoformat()} for c in read.calls.values()]
    return {"at": read.at, "tail": read.tail, "calls": calls, "done": sorted(read.done), "refusals": read.refusals}


def _from_plain(raw: dict[str, Any]) -> _Read:
    calls = [AgentCall(**{**c, "started": datetime.fromisoformat(c["started"])}) for c in raw["calls"]]
    refusals = {str(use): [str(r) for r in found] for use, found in raw["refusals"].items()}
    return _Read(int(raw["at"]), str(raw["tail"]), {c.tool_use_id: c for c in calls}, set(raw["done"]), refusals)


def _delivered(entry: dict[str, Any]) -> list[str]:
    """What reached the session from outside its own turn: a user message, or one queued while the session worked.

    A hand-back or notice that arrives mid-turn is written only as a queue operation and a queued-command attachment.
    """
    kind = entry.get("type")
    if kind == "queue-operation" and entry.get("operation") == "enqueue":
        return [str(entry.get("content", ""))]
    attachment = entry.get("attachment")
    if kind == "attachment" and isinstance(attachment, dict) and attachment.get("type") == "queued_command":
        return [str(attachment.get("prompt", ""))]
    if kind != "user":
        return []
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [content]
    return [str(b.get("text", "")) for b in transcript.blocks(entry) if b.get("type") == "text"]


def _started(block: dict[str, Any], when: datetime) -> AgentCall:
    raw = block.get("input")
    args = raw if isinstance(raw, dict) else {}
    return AgentCall(
        tool_use_id=str(block.get("id", "")),
        subagent_type=str(args.get("subagent_type") or "general-purpose"),
        started=when,
        prompt=str(args.get("prompt", "")),
        model=str(args.get("model") or ""),
        agent_id=None,
        finished=False,
    )


def _resulted(pending: AgentCall | None, result: str) -> None:
    """An Agent call's result: a background launch names its agent id; any other result means it finished."""
    if pending is None:
        return
    launched = _AGENT_ID.search(result)
    if launched and "Async agent launched" in result:
        pending.agent_id = launched.group(1)
    else:
        pending.finished = True


def _note_done(text: str, done: set[str]) -> None:
    if "<task-notification>" in text and _DONE.search(text):
        done.update(_NOTICE_ID.findall(text))
    done.update(_HANDBACK_ID.findall(text))
