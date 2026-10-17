"""The factory's MCP at /mcp: Claude reads the factory and acts on it as the owner, with a signed-in device's token.

Streamable HTTP with no session and no event stream: each POST is one JSON-RPC message, a request's answer is the
response's JSON body, and a notification gets 202 with none. Two tools: ``snapshot`` reads what the dashboard reads,
and ``call`` runs any verb the owner socket answers, as the dashboard's buttons do. The dashboard's route checks the
address and the token first; this module only speaks the protocol.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from runtime.dashboard.client import Warden, WardenDown
from runtime.warden.verbs import VERBS

PATH = "/mcp"
# Newest first. A client's own version is echoed when it is here; else the newest is offered and the client decides.
_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
_PARSE_ERROR, _INVALID_REQUEST, _NO_METHOD, _INVALID_PARAMS = -32700, -32600, -32601, -32602
_SERVER = {"name": "software-factory", "version": "1"}  # the protocol requires a version; nothing reads it
_NO_ANSWER = (
    "The warden did not answer, so whether this ran is unknown. Read the snapshot, or send it again with the same "
    "idempotency_key."
)

_TOOLS: dict[str, dict[str, Any]] = {
    "snapshot": {
        "description": "Read the factory as the dashboard does: its state, agents, queue, settings, Self-reflect "
        "and more, each as the warden keeps it. Send an empty want to have the warden list every name it knows.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "want": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The parts to read, such as factory, sessions, queue, settings and reflections. "
                    "Any other argument the warden's snapshot reads, such as tasks, goes with it unchanged.",
                },
            },
            "required": ["want"],
        },
    },
    "call": {
        "description": "Run one of the owner's actions through the warden, as the dashboard's buttons do, and get "
        "the warden's answer as JSON; a refusal says why. A verb's args are what its handler in runtime/ reads. "
        "An idempotency_key in args runs a retried action once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "verb": {"type": "string", "enum": sorted(name for name, verb in VERBS.items() if verb.owner_socket)},
                "args": {"type": "object"},
                "data": {"type": "string", "description": "Text for a verb that takes bytes: account.add's token."},
            },
            "required": ["verb", "args"],
        },
    },
}


class _Invalid(Exception):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        super().__init__(message)


def answer(warden: Warden, lost: Callable[[], None], raw: bytes) -> tuple[int, dict[str, Any] | None]:
    """The HTTP status and the JSON-RPC answer to one message, or None for a notification, which gets none.
    ``lost`` reports that the warden did not answer."""
    try:
        message = json.loads(raw)
    # telemetry: surfaced — the client is answered with `_error`, a parse error
    except ValueError:
        return 400, _error(None, _PARSE_ERROR, "The body is not JSON.")
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return 400, _error(None, _INVALID_REQUEST, "Send one JSON-RPC 2.0 message in each POST.")
    if "id" not in message:
        return 202, None
    ident, method, params = message["id"], message.get("method"), message.get("params")
    if isinstance(ident, bool) or not isinstance(ident, str | int) or not isinstance(method, str):
        return 400, _error(None, _INVALID_REQUEST, "A request carries a string or number id, and a method.")
    try:
        if not isinstance(params, dict | None):
            raise _Invalid(_INVALID_PARAMS, "The params are one object.")
        result = _result(warden, lost, method, params or {})
    # telemetry: surfaced — the client is answered with `_error`, the JSON-RPC error
    except _Invalid as exc:
        return 200, _error(ident, exc.code, str(exc))
    return 200, {"jsonrpc": "2.0", "id": ident, "result": result}


def _result(warden: Warden, lost: Callable[[], None], method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        asked = params.get("protocolVersion")
        if not isinstance(asked, str):
            raise _Invalid(_INVALID_PARAMS, "initialize names the client's protocolVersion.")
        version = asked if asked in _VERSIONS else _VERSIONS[0]
        return {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": _SERVER}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": name, **tool} for name, tool in _TOOLS.items()]}
    if method == "tools/call":
        return _tool(warden, lost, params)
    # Claude Code first asks for a newer protocol's server/discover, and on this answer falls back to initialize.
    raise _Invalid(_NO_METHOD, f"There is no method {method}.")


def _tool(warden: Warden, lost: Callable[[], None], params: dict[str, Any]) -> dict[str, Any]:
    name, arguments = params.get("name"), params.get("arguments")
    if name not in _TOOLS:
        raise _Invalid(_INVALID_PARAMS, f"There is no tool {name}.")
    if not isinstance(arguments, dict):
        return _text("The tool's arguments are one object.", error=True)
    sent = {"verb": "snapshot", "args": arguments} if name == "snapshot" else arguments
    verb, args, data = sent.get("verb"), sent.get("args"), sent.get("data")
    if not isinstance(verb, str) or not isinstance(args, dict) or not isinstance(data, str | None):
        return _text("call takes verb, a string; args, an object; and data, text, if the verb takes it.", error=True)
    try:
        reply = warden.call(verb, args, None if data is None else data.encode())
    # telemetry: surfaced — `lost` reports the warden's absence once; the tool's answer says what is unknown
    except WardenDown:
        lost()
        return _text(_NO_ANSWER, error=True)
    return _text(json.dumps(reply), error=not reply["ok"])


def _text(text: str, error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _error(ident: str | int | None, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}
