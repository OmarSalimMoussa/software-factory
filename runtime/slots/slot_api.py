"""`factory slot-api`: a tiny MCP server, over standard input and output, that gives a session HTTP access to its
own slot's API and nothing else (factory spec, "Reaching a slot").

Before each request it asks the warden which slot is the caller's, then sends it to
``http://127.0.0.1:<that slot's port><path>``: no other host, no redirects, a path only, and an answer cut at the
warden's size. Nothing but MCP goes to stdout. A request that names a session sends the cookies the slot set in that
session before, by RFC 6265's paths; an answer shows every header line, each Set-Cookie value hidden. The tool frames
each request and closes its connection itself, so a header that would change either is refused, saying the way that
works; the one Connection header it sends is a websocket upgrade's, whose 101 answer shows its head alone.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import IO, Any
from urllib.parse import urlsplit

from runtime.shared import network

PROTOCOL = "2025-06-18"
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
PATH = re.compile(r"^/[\x21-\x7e]*$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
UPGRADE = (
    "a websocket upgrade check: method GET and no body, `Connection: Upgrade` with `Upgrade: websocket`, "
    "`Sec-WebSocket-Version: 13` and a `Sec-WebSocket-Key`"
)
SET_BY_TOOL = {
    "host": "the tool sends each request to your slot's own address",
    "transfer-encoding": "the tool frames the body itself: send it as `body`",
    "content-length": "the tool frames the body itself: send it as `body`",
    "proxy-authorization": "no proxy stands between the tool and your slot",
    "connection": f"the tool closes each connection after its answer; the one Connection header it sends is {UPGRADE}",
}
SESSION = re.compile(r"^[A-Za-z0-9@._+-]{1,200}$")
TOOL = {
    "name": "slot_api",
    "description": "Send one HTTP request to your own stack slot's API and read the answer.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "method": {"type": "string", "enum": list(METHODS)},
            "path": {"type": "string", "description": "the path and query, starting with /"},
            "headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": f"header names to values. For {UPGRADE}: a 101 answer shows its status and headers, "
                "then the tool closes the connection",
            },
            "body": {"type": "string"},
            "session": {
                "type": "string",
                "description": "a name such as your test address: the cookies the slot sets are kept under it and "
                "sent with each later request that names it; with none, or with a Cookie header of your own, no "
                "cookie is kept or sent",
            },
        },
        "required": ["method", "path"],
    },
}


class SlotApiError(ValueError):
    """A request the tool will not send; the message says why."""


@dataclass(slots=True)
class Session:
    name: str
    cookies: dict[tuple[str, str], str]  # (name, path): value

    def header(self, path: str) -> str:
        found = sorted(((p, n, v) for (n, p), v in self.cookies.items() if _within(path, p)), key=lambda c: -len(c[0]))
        return "; ".join(f"{n}={v}" for _, n, v in found)


def _within(path: str, cookie_path: str) -> bool:
    if path == cookie_path:
        return True
    return path.startswith(cookie_path) and (cookie_path.endswith("/") or path[len(cookie_path)] == "/")


def build(port: int, arguments: dict[str, Any]) -> urllib.request.Request:
    method, path = arguments.get("method"), arguments.get("path")
    if method not in METHODS:
        raise SlotApiError(f"method is one of {', '.join(METHODS)}")
    if not isinstance(path, str) or not PATH.match(path) or path.startswith("//"):
        raise SlotApiError("path starts with one / and holds no spaces or control characters")
    body = arguments.get("body")
    if body is not None and not isinstance(body, str):
        raise SlotApiError("body is text")
    headers = _headers(arguments.get("headers", {}), upgrade=method == "GET" and body is None)
    try:
        data = body.encode() if body is not None else None
    except UnicodeEncodeError as exc:
        raise SlotApiError(f"body holds a character UTF-8 cannot carry, at {exc.start}") from exc
    return urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method)


def _headers(headers: object, *, upgrade: bool) -> dict[str, str]:
    """The headers asked for, each one line of Latin-1 and none the tool sets itself, but for a websocket upgrade's
    Connection when ``upgrade`` (a request that may switch protocols)."""
    if not isinstance(headers, dict):
        raise SlotApiError("headers map names to text")
    for name, value in headers.items():
        if not HEADER_NAME.match(str(name)):
            raise SlotApiError(f"the header {name!r} cannot be sent")
        if not isinstance(value, str) or any(c in value for c in "\r\n\0") or any(ord(c) > 255 for c in value):
            raise SlotApiError(f"the header {name} holds one line of Latin-1 text")
    asked = {name.lower(): value.lower() for name, value in headers.items()}
    upgrade = upgrade and asked.get("connection") == "upgrade" and asked.get("upgrade") == "websocket"
    for name in headers:
        why = SET_BY_TOOL.get(name.lower())
        if why is not None and not (upgrade and name.lower() == "connection"):
            raise SlotApiError(f"the header {name!r} is not yours to send: {why}")
    return headers


def send(request: urllib.request.Request, timeout: int, limit: int, session: Session | None) -> str:
    """The answer: its status, every header (each cookie kept in ``session``, its value hidden) and its body, or for a
    switch of protocols, that the tool closed the connection."""
    door = network.upgrade if request.has_header("Connection") else network.call
    answer = door(request, timeout, limit=limit + 1)
    path = urlsplit(request.full_url).path
    head = [f"HTTP {answer.status}"]
    for name, value in answer.headers.items():
        head += [f"{name}: {_cookie(v, session, path) if name == 'set-cookie' else v}" for v in value.split("\n")]
    if session is not None:
        names = ", ".join(dict.fromkeys(name for name, _ in session.cookies))
        head.append(f"[session {session.name} holds: {names or 'no cookies'}]")
    raw = answer.body
    text = raw[:limit].decode(errors="replace") + ("\n[cut: the answer is longer]" if len(raw) > limit else "")
    if answer.status == HTTPStatus.SWITCHING_PROTOCOLS:
        text = "[the slot switched protocols; the tool read no further and closed the connection]"
    return "\n".join(head) + f"\n\n{text}"


def _cookie(line: str, session: Session | None, path: str) -> str:
    """One Set-Cookie line, its value hidden; ``session`` keeps the cookie, or drops it when the line expires it. Its
    path is the line's, or else the request's up to its last /, as RFC 6265 has it."""
    pair, *attributes = line.split(";")
    name, sign, value = (part.strip() for part in pair.partition("="))
    if session is not None and sign and name:
        found = {k.strip().lower(): v.strip() for k, _, v in (a.partition("=") for a in attributes)}
        where = found.get("path", "")
        key = (name, where if where.startswith("/") else path[: path.rindex("/")] or "/")
        if _expired(found):
            session.cookies.pop(key, None)
        else:
            session.cookies[key] = value
    return ";".join([f"{name}=(hidden)", *attributes])


def _expired(attributes: dict[str, str]) -> bool:
    """Max-Age at or below zero, or else an Expires in the past, as a browser reads them."""
    age = attributes.get("max-age", "")
    if age.lstrip("-").isdigit():
        return int(age) <= 0
    try:
        return "expires" in attributes and parsedate_to_datetime(attributes["expires"]).timestamp() <= time.time()
    # telemetry: expected — an Expires that is no date leaves the cookie kept, as a browser does
    except (TypeError, ValueError):
        return False


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


def caller(which: Callable[[], dict[str, Any]]) -> Callable[[dict[str, Any]], str]:
    """Each request asks the warden for the caller's slot first: a session claims, releases or changes its slot while
    the tool runs, and Claude Code never starts again a server that exited."""
    sessions: dict[tuple[int, str], Session] = {}

    def call(arguments: dict[str, Any]) -> str:
        name = arguments.get("session")
        if name is not None and (not isinstance(name, str) or not SESSION.match(name)):
            raise SlotApiError("session is one word of letters, digits and @._+-, such as your test address")
        try:
            found = which()
        except ValueError as exc:
            raise SlotApiError(f"the warden's answer could not be read ({type(exc).__name__}): try again") from exc
        if not found.get("ok"):
            raise SlotApiError(f"you hold no slot: {found.get('reason', 'the warden refused')}")
        port, timeout, limit = int(found["port"]), int(found["timeout_seconds"]), int(found["max_kib"]) * 1024
        request = build(port, arguments)
        session = None if name is None else sessions.setdefault((port, name), Session(name, {}))
        if request.has_header("Cookie"):
            session = None
        elif session is not None and (cookies := session.header(urlsplit(request.full_url).path)):
            request.add_header("Cookie", cookies)
        return send(request, timeout, limit, session)

    return call


def main(which: Callable[[], dict[str, Any]]) -> int:
    """Serve until Claude Code closes standard input."""
    serve(sys.stdin, sys.stdout, caller(which))
    return 0
