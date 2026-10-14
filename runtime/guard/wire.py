"""What the hook must know before any rule loads: how a refusal reads, which calls only read or report, and how a
refusal reaches the warden. Stdlib only, so a broken rule module can never stop the hook from refusing."""

from __future__ import annotations

import errno
import json
import socket
from pathlib import Path

from runtime.guard import deadline

# The text before every refusal, which Claude Code hands back as the call's result: transcripts are read for it.
REFUSED = "Refused by the factory guard"
# How a background subagent hands its report to its parent: a rule that stops a subagent still lets this through.
HANDBACK = "SubagentHandback"
READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS", "ToolSearch", "TaskOutput", "TodoWrite", HANDBACK})
PARENTS = frozenset({"main", "orchestrator"})
# A refusal line's fields beside its time; a subagent's id and kind are empty on its parent's own calls.
REFUSAL_FIELDS = ("session", "agent_id", "agent_type", "rule", "tool", "detail")


def one_line(rule: str, reason: str) -> str:
    return f"{REFUSED} ({rule}): {' '.join(reason.split())}"


def simple_way_out(payload: dict[str, object]) -> bool:
    """Reading and reporting, known from the input alone, so a broken rule never blocks them."""
    tool = payload.get("tool_name")
    raw = payload.get("tool_input")
    to_parent = tool == "SendMessage" and isinstance(raw, dict) and raw.get("to") in PARENTS
    return tool in READ_TOOLS or to_parent


def send_refusal(path: Path, record: dict[str, str]) -> None:
    """One JSON line to the warden's refusal socket, never waiting on it: the refusal stands either way, and a
    warden that is down misses only this line of its count."""
    line = (json.dumps(record, sort_keys=True) + "\n").encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.setblocking(False)
            sock.sendto(line, str(path))
            return
    # telemetry: expected — the refusal stands; a warden that is down misses one count line, and the dashboard says so
    except OSError as exc:
        if exc.errno != errno.EPROTOTYPE:
            return
    # The warden listens on a stream socket instead.
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(deadline.spare())
            sock.connect(str(path))
            sock.sendall(line)
    # telemetry: expected — the refusal stands; a warden that is down misses one count line, and the dashboard says so
    except OSError:
        return
