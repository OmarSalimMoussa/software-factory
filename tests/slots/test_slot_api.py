"""The slot-api tool reaches its own slot's port on the loopback and nothing else, and keeps a named session's
cookies without showing them."""

from __future__ import annotations

import http.server
import io
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from runtime.slots import slot_api
from runtime.slots.slot_api import SlotApiError, build, serve


@pytest.mark.parametrize(
    "arguments",
    [
        {"method": "GET", "path": "http://evil.example/"},
        {"method": "GET", "path": "//evil.example/x"},
        {"method": "GET", "path": "/a b"},
        {"method": "GET", "path": "@evil.example/"},
        {"method": "GET", "path": "/", "headers": {"Host": "evil.example"}},
        {"method": "GET", "path": "/", "headers": {"X-A": "1\r\nX-B: 2"}},
        {"method": "GET", "path": "/", "headers": {"X-Name": "\u540d\u524d"}},
        {"method": "CONNECT", "path": "/"},
    ],
)
def test_a_request_that_could_leave_the_slot_is_not_sent(arguments: dict[str, object]) -> None:
    with pytest.raises(SlotApiError):
        build(8101, arguments)


def test_a_request_goes_to_the_slots_own_port() -> None:
    request = build(8102, {"method": "POST", "path": "/api/items?x=1", "body": "{}"})
    assert (request.full_url, request.get_method(), request.data) == (
        "http://127.0.0.1:8102/api/items?x=1",
        "POST",
        b"{}",
    )


def test_the_server_speaks_mcp_over_stdio() -> None:
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "slot_api", "arguments": {"path": "/"}}},
    ]
    out = io.StringIO()
    serve(io.StringIO("".join(json.dumps(m) + "\n" for m in lines)), out, lambda a: build(8101, a).full_url)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[1]["result"]["tools"][0]["name"] == "slot_api"
    assert replies[2]["result"]["isError"] is True  # no method named


def test_each_request_goes_to_the_slot_the_caller_holds_at_that_moment(monkeypatch: pytest.MonkeyPatch) -> None:
    held = {"ok": True, "timeout_seconds": 30, "max_kib": 256}
    answers = iter([{"ok": False, "reason": "claim a slot first"}, {**held, "port": 8101}, {**held, "port": 8102}])
    monkeypatch.setattr(slot_api, "send", lambda request, timeout, limit, session: request.full_url)
    call = slot_api.caller(lambda: next(answers))

    with pytest.raises(SlotApiError, match="claim a slot first"):
        call({"method": "GET", "path": "/"})
    assert [call({"method": "GET", "path": "/"}) for _ in range(2)] == [
        "http://127.0.0.1:8101/",
        "http://127.0.0.1:8102/",
    ]


_SETS = {
    "/signin/": ["sid={who}-token; Path=/; HttpOnly", "csrf=c1; Expires=Thu, 01 Jan 2099 00:00:00 GMT; Path=/",
                 "pref=dark; Path=/settings", "junk"],
    "/signout/admin": ["sid=; Path=/admin; Max-Age=0"],
    "/signout/all": ["sid=; Max-Age=soon; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/"],
}  # fmt: skip


class _Slot(http.server.BaseHTTPRequestHandler):
    """Every page answers with the cookies it was sent."""

    def do_GET(self) -> None:
        signing_in = self.path.startswith("/signin/")
        self.send_response(302 if signing_in else 200)
        if signing_in:
            self.send_header("Location", "/")
            self.send_header("X-Note", "a")
            self.send_header("X-Note", "b")
        for line in _SETS["/signin/" if signing_in else self.path] if signing_in or self.path in _SETS else []:
            self.send_header("Set-Cookie", line.format(who=self.path.removeprefix("/signin/")))
        body = (self.headers.get("Cookie") or "signed out").encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def slot_port() -> Iterator[int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Slot)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_a_named_session_keeps_its_cookies_apart_and_never_shows_their_values(slot_port: int) -> None:
    call = slot_api.caller(lambda: {"ok": True, "port": slot_port, "timeout_seconds": 5, "max_kib": 64})

    def sent(path: str, session: str | None = None, **more: Any) -> str:
        """What the page answered: the cookies the request carried."""
        named = {"session": session} if session else {}
        return call({"method": "GET", "path": path, **named, **more}).split("\n\n", 1)[1]

    shown = call({"method": "GET", "path": "/signin/ada", "session": "ada@notes.test"})
    assert (
        "HTTP 302" in shown and "location: /" in shown and "ada-token" not in shown and "x-note: a\nx-note: b" in shown
    )
    assert (
        "set-cookie: sid=(hidden); Path=/; HttpOnly" in shown
        and "[session ada@notes.test holds: sid, csrf, pref]" in shown
    )
    sent("/signin/bob", "bob@notes.test")
    assert sent("/me", "ada@notes.test") == "sid=ada-token; csrf=c1"
    assert sent("/settings/theme", "ada@notes.test") == "pref=dark; sid=ada-token; csrf=c1"
    assert sent("/me", "bob@notes.test") == "sid=bob-token; csrf=c1"
    assert sent("/me") == "signed out"
    assert sent("/signout/all", "ada@notes.test", headers={"Cookie": "sid=bob-token"}) == "sid=bob-token"
    sent("/signout/admin", "ada@notes.test")
    assert sent("/me", "ada@notes.test") == "sid=ada-token; csrf=c1"
    sent("/signout/all", "ada@notes.test")
    assert sent("/me", "ada@notes.test") == "csrf=c1"


@pytest.mark.parametrize("session", ["", "two words", "a;b", 7])
def test_a_session_is_one_name(session: object) -> None:
    call = slot_api.caller(lambda: {"ok": True, "port": 8101, "timeout_seconds": 5, "max_kib": 64})
    with pytest.raises(SlotApiError, match="session is one word"):
        call({"method": "GET", "path": "/", "session": session})


def test_a_body_utf8_cannot_carry_is_refused_and_the_server_answers_on() -> None:
    lines = [
        {"jsonrpc": "2.0", "id": n, "method": "tools/call",
         "params": {"name": "slot_api", "arguments": {"method": "POST", "path": "/", "body": body}}}
        for n, body in ((1, "\ud800"), (2, "{}"))
    ]  # fmt: skip
    out = io.StringIO()
    serve(io.StringIO("".join(json.dumps(m) + "\n" for m in lines)), out, lambda a: build(8101, a).full_url)
    replies = [json.loads(line)["result"] for line in out.getvalue().splitlines()]
    assert replies[0]["isError"] and "UTF-8" in replies[0]["content"][0]["text"] and not replies[1]["isError"]
