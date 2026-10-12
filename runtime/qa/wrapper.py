"""The tester's browser wrapper: it stands between the tester's session and Playwright's MCP server, so every run
leaves a Playwright trace the tester neither starts nor can stop, and the tester has the factory's own browser tools
beside Playwright's.

Right after the session opens, the wrapper starts the trace itself and swallows the answer; a trace that does not
start stops the browser, so nothing is tested untraced. It drops the trace's tools and the rest of the devtools
capability from the tool list, adds the factory's tools (wrapper_tools.py) and answers those itself, calling the
server as a client would. Every other message passes through unchanged. Nothing but MCP goes to standard output.
When the server stops, the wrapper stops too, so the session sees its browser gone.
"""

from __future__ import annotations

import itertools
import json
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any

# The devtools capability holds the trace's tools; the wrapper needs the capability on, the tester keeps its usual
# tools, so every tool the capability adds is hidden. Closing the browser would end the trace, and the next call
# would open an untraced browser, so that is hidden too: the browser closes with the session.
HIDDEN = frozenset(
    {
        "browser_start_tracing",
        "browser_stop_tracing",
        "browser_annotate",
        "browser_highlight",
        "browser_hide_highlight",
        "browser_resume",
        "browser_start_video",
        "browser_stop_video",
        "browser_video_chapter",
        "browser_video_hide_actions",
        "browser_video_show_actions",
        "browser_close",
    }
)
OWN = "factory-"  # the ids of the wrapper's own requests to the server
# A factory tool: its name, its arguments, and an event set when the session cancels the call; (text, failed).
Answer = Callable[[str, dict[str, Any], threading.Event], tuple[str, bool]]


class TraceError(Exception):
    """The trace did not start; the browser stops rather than test untraced."""


class ServerError(Exception):
    """A call the wrapper made to the server failed or was not answered in time; the message says which."""


@dataclass
class _Waiting:
    done: threading.Event = field(default_factory=threading.Event)
    answer: dict[str, Any] = field(default_factory=dict)


class Proxy:
    """One session's MCP traffic: the client is the tester's session, the server is Playwright's."""

    def __init__(self, server_in: IO[str], client_out: IO[str], wait_seconds: float) -> None:
        self._server_in, self._client_out, self._wait = server_in, client_out, wait_seconds
        self._tools: list[dict[str, Any]] = []
        self._answer_tool: Answer = lambda name, args, cancelled: ("", True)
        self._server_lock, self._client_lock = threading.Lock(), threading.Lock()
        self._mine: dict[str, _Waiting] = {}
        self._lists: set[Any] = set()
        self._running: dict[str, threading.Event] = {}  # the factory tools at work, by the session's request id
        self._ids = itertools.count(1)
        self._secrets: list[str] = []

    def add_tools(self, tools: list[dict[str, Any]], answer: Answer) -> None:
        """The factory's own tools, listed beside the server's and answered by ``answer``."""
        self._tools, self._answer_tool = tools, answer

    def hide(self, secret: str) -> None:
        """From now on ``secret`` never reaches the session: a page left on a sign-in link names it in every answer."""
        with self._client_lock:
            self._secrets.append(secret)

    def from_server(self, line: str) -> None:
        message = json.loads(line)
        key = message.get("id")
        if isinstance(key, str) and key.startswith(OWN):
            # An answer that came after its request gave up waiting is nobody's: the session never asked.
            mine = self._mine.pop(key, None)
            if mine is not None:
                mine.answer = message
                mine.done.set()
            return
        if key in self._lists and isinstance(message.get("result"), dict):
            self._lists.discard(key)
            listed = [t for t in message["result"].get("tools", []) if t.get("name") not in HIDDEN]
            message["result"]["tools"] = listed + self._tools
            line = json.dumps(message)
        self._to_client(line)

    def from_client(self, line: str) -> None:
        message = json.loads(line)
        method, params = message.get("method"), message.get("params") or {}
        if method == "tools/list":
            self._lists.add(message.get("id"))
        if method == "tools/call" and params.get("name") in HIDDEN:
            self._error(message.get("id"), f"there is no tool {params['name']}")
            return
        if method == "tools/call" and any(t["name"] == params.get("name") for t in self._tools):
            cancelled = self._running[str(message.get("id"))] = threading.Event()
            # A wait may last minutes: it runs on its own, so the session's other calls still pass.
            threading.Thread(target=self._factory_tool, args=(message, cancelled), daemon=True).start()
            return
        running = self._running.get(str(params.get("requestId"))) if method == "notifications/cancelled" else None
        if running is not None:
            running.set()  # the server never saw that request
            return
        self._to_server(line)
        if method == "notifications/initialized":
            self.start_trace()

    def start_trace(self) -> None:
        """Before the session's first call: the server takes calls in order, so nothing runs untraced."""
        try:
            answer = self.request("tools/call", {"name": "browser_start_tracing", "arguments": {}})
        except ServerError as exc:
            raise TraceError(str(exc)) from exc
        if answer.get("isError"):
            raise TraceError(text_of(answer)[:400])

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one request of the wrapper's own and wait for its answer's result."""
        key = f"{OWN}{next(self._ids)}"
        waiting = _Waiting()
        self._mine[key] = waiting
        self._to_server(json.dumps({"jsonrpc": "2.0", "id": key, "method": method, "params": params}))
        if not waiting.done.wait(self._wait):
            self._mine.pop(key, None)
            raise ServerError(f"the browser did not answer {method} within {self._wait:.0f} s")
        if "error" in waiting.answer:
            raise ServerError(f"the browser refused {method}: {json.dumps(waiting.answer['error'])[:300]}")
        result = waiting.answer.get("result")
        return result if isinstance(result, dict) else {}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """One of the server's tools, for a factory tool: its text, or ServerError when it failed."""
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise ServerError(f"{name} failed: {text_of(result)[:300]}")
        return text_of(result)

    def _factory_tool(self, message: dict[str, Any], cancelled: threading.Event) -> None:
        params = message.get("params") or {}
        given, name = params.get("arguments"), str(params.get("name"))
        arguments: dict[str, Any] = given if isinstance(given, dict) else {}
        try:
            text, failed = self._answer_tool(name, arguments, cancelled)
        # telemetry: surfaced — the session reads the break as the call's failed `text`; `sys.stderr` names its class
        except Exception as exc:  # noqa: BLE001 - a factory tool that breaks must still answer, or the session hangs
            print(f"factory qa-browser: {name} broke: {type(exc).__name__}: {exc}", file=sys.stderr)
            text, failed = f"the factory's {name} broke ({type(exc).__name__}); report the step blocked with that", True
        finally:
            self._running.pop(str(message.get("id")), None)
        if cancelled.is_set():
            return  # a cancelled request gets no answer
        result = {"content": [{"type": "text", "text": text}], **({"isError": True} if failed else {})}
        self._to_client(json.dumps({"jsonrpc": "2.0", "id": message.get("id"), "result": result}))

    def _error(self, key: Any, text: str) -> None:
        self._to_client(json.dumps({"jsonrpc": "2.0", "id": key, "error": {"code": -32602, "message": text}}))

    def _to_server(self, line: str) -> None:
        with self._server_lock:
            self._server_in.write(line.rstrip("\n") + "\n")
            self._server_in.flush()

    def _to_client(self, line: str) -> None:
        with self._client_lock:
            for secret in self._secrets:
                line = line.replace(secret, "the sign-in link")
            self._client_out.write(line.rstrip("\n") + "\n")
            self._client_out.flush()


def text_of(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text")


def serve(
    argv: list[str],
    equip: Callable[[Proxy], None],
    wait_seconds: float,
    client_in: IO[str] = sys.stdin,
    client_out: IO[str] = sys.stdout,
) -> int:
    """Run the server as a child, let ``equip`` add the factory's tools, and carry the session's traffic until the
    session closes standard input or the server stops; the server's exit code, or 1 when the trace did not start."""
    server = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    assert server.stdin is not None and server.stdout is not None  # both were asked for as pipes
    proxy = Proxy(server.stdin, client_out, wait_seconds)
    equip(proxy)
    ended, why = threading.Event(), []

    def pump() -> None:
        assert server.stdout is not None  # asked for as a pipe
        for line in server.stdout:
            if line.strip():
                proxy.from_server(line)
        why.append("server")
        ended.set()

    def feed() -> None:
        try:
            for line in client_in:
                if line.strip():
                    proxy.from_client(line)
            why.append("session")
        # telemetry: surfaced — `why` carries it to `serve`, which says it on `sys.stderr` and returns 1
        except TraceError as exc:
            why.append(f"the trace did not start, so this browser stops: {exc}")
        # telemetry: surfaced — the server is gone; `why` says so, and `serve` reports it on stderr
        except OSError:
            why.append("server")
        ended.set()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    threading.Thread(target=feed, daemon=True).start()
    ended.wait()
    if why[0] == "session":
        server.stdin.close()
        reader.join()  # the server's last answers still reach the session
        return server.wait()
    server.kill()
    said = why[0] if why[0] != "server" else f"the browser stopped (exit {server.wait()})"
    print(f"factory qa-browser: {said}", file=sys.stderr)
    return 1
