"""`factory slot-api`: a tiny MCP server, over standard input and output, that gives a session HTTP access to its
own slot's API and nothing else (factory spec, "Reaching a slot").

A sandboxed command cannot reach the host's loopback, so Claude Code starts this tool outside the sandbox. It asks
the warden which slot is the caller's, then sends each request to ``http://127.0.0.1:<that slot's port><path>``:
no other host, no redirects, a path only, and an answer cut at the warden's size. Nothing but MCP goes to stdout.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import IO, Any

from runtime.shared import network

PROTOCOL = "2025-06-18"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
PATH = re.compile(r"^/[\x21-\x7e]*$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
BLOCKED_HEADERS = frozenset({"host", "connection", "transfer-encoding", "content-length", "proxy-authorization"})
TOOL = {
    "name": "slot_api",
    "description": "Send one HTTP request to your own stack slot's API and read the answer.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": list(METHODS)},
            "path": {"type": "string", "description": "the path and query, starting with /"},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "body": {"type": "string"},
        },
        "required": ["method", "path"],
    },
}


class SlotApiError(ValueError):
    """A request the tool will not send; the message says why."""


def build(port: int, arguments: dict[str, Any]) -> urllib.request.Request:
    method, path = arguments.get("method"), arguments.get("path")
    if method not in METHODS:
        raise SlotApiError(f"method is one of {', '.join(METHODS)}")
    if not isinstance(path, str) or not PATH.match(path) or path.startswith("//"):
        raise SlotApiError("path starts with one / and holds no spaces or control characters")
    headers = arguments.get("headers", {})
    if not isinstance(headers, dict):
        raise SlotApiError("headers map names to text")
    for name, value in headers.items():
        if not HEADER_NAME.match(str(name)) or name.lower() in BLOCKED_HEADERS:
            raise SlotApiError(f"the header {name!r} cannot be sent")
        if not isinstance(value, str) or any(c in value for c in "\r\n\0"):
            raise SlotApiError(f"the header {name} holds one line of text")
    body = arguments.get("body")
    if body is not None and not isinstance(body, str):
        raise SlotApiError("body is text")
    data = body.encode() if body is not None else None
    return urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)


def send(request: urllib.request.Request, timeout: int, limit: int) -> str:
    answer = network.call(request, timeout, limit=limit + 1)
    status, raw = answer.status, answer.body
    kind, location = answer.headers.get("content-type", ""), answer.headers.get("location", "")
    text = raw[:limit].decode(errors="replace") + ("\n[cut: the answer is longer]" if len(raw) > limit else "")
    head = (
        f"HTTP {status}" + (f"\nContent-Type: {kind}" if kind else "") + (f"\nLocation: {location}" if location else "")
    )
    return f"{head}\n\n{text}"


def handle(message: dict[str, Any], call: Callable[[dict[str, Any]], str]) -> dict[str, Any] | None:
    """The answer to one JSON-RPC message; None for a notification."""
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        return None
    if method == "initialize":
        version = message.get("params", {}).get("protocolVersion", PROTOCOL)
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "slot-api", "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = message.get("params", {})
        if params.get("name") != TOOL["name"]:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32602, "message": "unknown tool"}}
        try:
            result = {"content": [{"type": "text", "text": call(params.get("arguments", {}))}], "isError": False}
        # telemetry: surfaced — the session's tool `result` is an error saying what was not sent
        except (SlotApiError, OSError) as exc:
            result = {"content": [{"type": "text", "text": f"not sent: {exc}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}


def serve(lines: IO[str], out: IO[str], call: Callable[[dict[str, Any]], str]) -> None:
    for line in lines:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        # telemetry: surfaced — the session is sent a `reply` saying its line is not JSON
        except ValueError:
            reply: dict[str, Any] | None = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "not JSON"},
            }
        else:
            reply = handle(message, call) if isinstance(message, dict) else None
        if reply is not None:
            out.write(json.dumps(reply) + "\n")
            out.flush()


def main(which: Callable[[], dict[str, Any]]) -> int:
    """Ask the warden for the caller's slot once, then serve until Claude Code closes standard input."""
    found = which()
    if not found.get("ok"):
        print(f"slot-api: no slot: {found.get('reason', 'the warden refused')}", file=sys.stderr)
        return 2
    port, timeout, limit = int(found["port"]), int(found["timeout_seconds"]), int(found["max_kib"]) * 1024
    serve(sys.stdin, sys.stdout, lambda arguments: send(build(port, arguments), timeout, limit))
    return 0
