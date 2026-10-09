"""The wire format between the `factory` command, the dashboard and the warden.

One JSON line in, one JSON line out. A request names a verb, its arguments and, for an upload, its bytes in base64
(the caller reads its own file; the warden never opens a path a caller names). An answer is either
``{"ok": true, ...}`` or ``{"ok": false, "refused": "<rule>", "reason": "<one line>"}``.
"""

from __future__ import annotations

import base64
import json
from typing import Any

MAX_LINE = 16 * 1024 * 1024


class Refused(Exception):
    """The warden refuses a request; ``rule`` names why, ``reason`` says what to do."""

    def __init__(self, rule: str, reason: str) -> None:
        self.rule, self.reason = rule, reason
        super().__init__(f"{rule}: {reason}")


def request(verb: str, args: dict[str, Any] | None = None, data: bytes | None = None, agent_id: str = "") -> bytes:
    """``agent_id``: the subagent the guard stamped on this call; the warden accepts it only from its parent."""
    body: dict[str, Any] = {"verb": verb, "args": args or {}}
    if agent_id:
        body["agent_id"] = agent_id
    if data is not None:
        body["bytes_b64"] = base64.b64encode(data).decode()
    return (json.dumps(body) + "\n").encode()


def parse_request(line: bytes) -> tuple[str, dict[str, Any], bytes | None, str]:
    if len(line) > MAX_LINE:
        raise Refused("protocol", "the request is too large")
    try:
        body = json.loads(line)
    except ValueError as exc:
        raise Refused("protocol", "the request is not JSON") from exc
    if not isinstance(body, dict) or not isinstance(body.get("verb"), str) or not isinstance(body.get("args"), dict):
        raise Refused("protocol", "a request needs a verb and args")
    raw = body.get("bytes_b64")
    data = base64.b64decode(raw) if isinstance(raw, str) else None
    agent_id = body.get("agent_id", "")
    if not isinstance(agent_id, str):
        raise Refused("protocol", "agent_id is a string")
    return body["verb"], body["args"], data, agent_id


def ok(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def refused(rule: str, reason: str) -> dict[str, Any]:
    return {"ok": False, "refused": rule, "reason": reason}


def answer(reply: dict[str, Any]) -> bytes:
    return (json.dumps(reply) + "\n").encode()
