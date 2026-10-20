"""The tester's browser wrapper: it stands between the tester's session and Playwright's MCP server, so every run
leaves a Playwright trace the tester neither starts nor can stop, and the tester has the factory's own browser tools
beside Playwright's.

Right after the session opens, the wrapper starts the trace itself and swallows the answer; a trace that does not
start stops the browser, so nothing is tested untraced. It drops the trace's tools and the rest of the devtools
capability from the tool list, adds the factory's tools (wrapper_tools.py) and answers those itself, calling the
server as a client would. Every other message passes through unchanged. Nothing but MCP goes to standard output.
A call of the session's the server leaves unanswered past ``qa.browser_answer_seconds``, plus a browser_wait_for's own
time, fails, and the server is told to stop it: a page script that never ends would hold the tester to the end.

Every tool but the close takes ``browser``: a call that names the second goes, without it, to the tester's second
browser (second.py), whose server has the wrapper as its client; the rest go to the first.
"""

from __future__ import annotations

import itertools
import json
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any, Protocol

from runtime.qa.calls import Calls

SERVER_CODE = "browser_run_code_unsafe"
WAIT_FOR = "browser_wait_for"
ROUTE = "browser_route"
# The input that names a file a server tool writes: the guard refuses it, so no listed tool offers it.
FILENAME = "filename"
CUT = (
    "\n\n[The factory cut this answer here: the page holds more than one answer can carry. Read what you need "
    "with browser_evaluate, or look with browser_take_screenshot.]"
)
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
        SERVER_CODE,  # the guard refuses it; browser_color_scheme does what testers needed it for
    }
)
OWN = "factory-"  # the ids of the wrapper's own requests to the server
CHOICE, FIRST, SECOND = "browser", "first", "second"
CLOSE = "browser_close_second"
FIELD = {
    "type": "string",
    "enum": [FIRST, SECOND],
    "description": "The browser to act in: first, your own, or second, a browser of its own sign-in, cookies and "
    f"storage, so a second person signs in beside the first. It opens on its first call; close it with {CLOSE}.",
}
# A factory tool: its name, its arguments, an event set when the session cancels it, and its browser; (text, failed).
Answer = Callable[[str, dict[str, Any], threading.Event, str], tuple[str, bool]]


class ServerError(Exception):
    """A call the wrapper made to the server failed or was not answered in time; the message says which."""


class SecondBrowser(Protocol):
    """The tester's second browser (second.py): ``send`` carries the session's lines in order once it is open,
    ``write`` the wrapper's own at once, and ``ready`` opens it or raises ServerError."""

    def send(self, line: str) -> None: ...
    def write(self, line: str) -> None: ...
    def ready(self) -> None: ...
    def stop(self) -> None: ...


@dataclass
class _Waiting:
    which: str
    done: threading.Event = field(default_factory=threading.Event)
    answer: dict[str, Any] = field(default_factory=dict)


def _seconds(params: dict[str, Any]) -> bool:
    """A wait for a time alone, which the factory's tools answer (wrapper_tools.py)."""
    args = params.get("arguments")
    return params.get("name") == WAIT_FOR and isinstance(args, dict) and set(args) == {"time"}


def _chosen(params: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """The browser a call names, and the call without it."""
    args = params.get("arguments")
    if not isinstance(args, dict) or CHOICE not in args:
        return FIRST, params
    return args[CHOICE], {**params, "arguments": {name: value for name, value in args.items() if name != CHOICE}}


class Proxy:
    """One session's MCP traffic: the client is the tester's session, the server is Playwright's."""

    def __init__(self, server_in: IO[str], client_out: IO[str], wait_seconds: float, answer_chars: int) -> None:
        self._server_in, self._client_out, self._wait, self._most = server_in, client_out, wait_seconds, answer_chars
        self._tools: list[dict[str, Any]] = []
        self._answer_tool: Answer = lambda name, args, cancelled, which: ("", True)
        self._notes: dict[str, str] = {}
        self._server_lock, self._client_lock = threading.Lock(), threading.Lock()
        self._mine: dict[str, _Waiting] = {}
        self._lists: set[Any] = set()
        self._running: dict[str, threading.Event] = {}  # the factory tools at work, by the session's request id
        self._calls = Calls(self._late)
        self._ids = itertools.count(1)
        self._secrets: list[str] = []
        self._second: SecondBrowser | None = None
        self.hello: dict[str, Any] = {}  # the session's initialize, which the second browser's server is sent too
        self.untraced: Callable[[str], None] = lambda why: None
        self._trace_asked, self._trace_ok, self._trace_answered = False, False, threading.Event()

    def add_tools(self, tools: list[dict[str, Any]], answer: Answer, notes: dict[str, str]) -> None:
        """The factory's own tools, listed beside the server's and answered by ``answer``; ``notes`` adds a line to
        one of the server's tools, by its name."""
        self._tools, self._answer_tool, self._notes = tools, answer, notes

    def add_second(self, second: SecondBrowser) -> None:
        self._second = second

    def _noted(self, tool: dict[str, Any]) -> dict[str, Any]:
        schema = tool.get("inputSchema")
        if isinstance(schema, dict) and isinstance(schema.get("properties"), dict) and FILENAME in schema["properties"]:
            kept = {name: field for name, field in schema["properties"].items() if name != FILENAME}
            tool = {**tool, "inputSchema": {**schema, "properties": kept}}
        note = self._notes.get(str(tool.get("name")))
        return tool if note is None else {**tool, "description": f"{tool.get('description', '')} {note}".strip()}

    def _either(self, tool: dict[str, Any]) -> dict[str, Any]:
        """A tool as listed: with the input naming the browser it acts in, once there is a second."""
        schema = tool.get("inputSchema")
        if self._second is None or tool.get("name") == CLOSE or not isinstance(schema, dict):
            return tool
        return {**tool, "inputSchema": {**schema, "properties": {**schema.get("properties", {}), CHOICE: FIELD}}}

    def hide(self, secret: str) -> None:
        """From now on ``secret`` never reaches the session: a page left on a sign-in link names it in every answer."""
        with self._client_lock:
            self._secrets.append(secret)

    def from_server(self, line: str) -> None:
        message = json.loads(line)
        key = message.get("id")
        if "method" not in message and not self._calls.arrived(key):
            return
        if isinstance(key, str) and key.startswith(OWN):
            # An answer that came after its request gave up waiting is nobody's: the session never asked.
            mine = self._mine.pop(key, None)
            if mine is not None:
                mine.answer = message
                mine.done.set()
            return
        if key in self._lists and isinstance(message.get("result"), dict):
            self._lists.discard(key)
            listed = [self._noted(t) for t in message["result"].get("tools", []) if t.get("name") not in HIDDEN]
            message["result"]["tools"] = [self._either(t) for t in listed + self._tools]
            line = json.dumps(message)
        elif isinstance(message.get("result"), dict) and _cut(message["result"].get("content"), self._most):
            line = json.dumps(message)
        self._to_client(line)

    def from_client(self, line: str) -> None:
        message = json.loads(line)
        method, params = message.get("method"), message.get("params") or {}
        if method == "initialize":
            self.hello = params
        if method == "tools/list":
            self._lists.add(message.get("id"))
        if method == "tools/call":
            self._call(message)
            return
        running = self._running.get(str(params.get("requestId"))) if method == "notifications/cancelled" else None
        if running is not None:
            running.set()  # the server never saw that request
            return
        which = FIRST
        if method == "notifications/cancelled":
            which = self._calls.where(params.get("requestId")) or FIRST
            self._calls.arrived(params.get("requestId"))
        self._carry(line, which)
        if method == "notifications/initialized":
            self.start_trace()

    def _call(self, message: dict[str, Any]) -> None:
        """A tool call of the session's: refused, answered here, or carried to the browser it names."""
        which, params = _chosen(message.get("params") or {})
        message, key = {**message, "params": params}, message.get("id")
        if which != FIRST and (which != SECOND or self._second is None):
            self._error(key, f"{CHOICE} is {FIRST} or {SECOND}")
        elif params.get("name") in HIDDEN:
            self._error(key, f"there is no tool {params['name']}")
        elif any(t["name"] == params.get("name") for t in self._tools) or _seconds(params):
            cancelled = self._running[str(key)] = threading.Event()
            # A wait may last minutes: it runs on its own, so the session's other calls still pass.
            threading.Thread(target=self._factory_tool, args=(message, cancelled, which), daemon=True).start()
        elif not self.traced():
            self._error(key, "the browser stopped: its trace did not start")
        else:
            self._time(key, params, which)
            self._carry(json.dumps(message), which)

    def _carry(self, line: str, which: str) -> None:
        """A line of the session's to the browser it names; the second takes it once it has opened."""
        if which == SECOND and self._second is not None:
            self._second.send(line)
        else:
            self._to_server(line)

    def _time(self, key: Any, params: dict[str, Any], which: str) -> None:
        """Answer a call for the server once it is past due: a page script that never ends would hold the tester."""
        args = params.get("arguments")
        given = args.get("time") if isinstance(args, dict) and params.get("name") == WAIT_FOR else None
        limit = self._wait + (given if isinstance(given, int | float) and not isinstance(given, bool) else 0)
        self._calls.sent(key, which, str(params.get("name")), limit)

    def _late(self, key: Any, name: str, limit: float) -> None:
        which = self._calls.give_up(key)
        if which is None:
            return
        stop = {"requestId": key, "reason": f"no answer within {limit:.0f} s"}
        self._carry(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": stop}), which)
        self._failed(key, (
            f"the browser did not answer {name} within {limit:.0f} s, so the factory stopped waiting for it: a page "
            "script must end on its own, and a wait tool waits for the page"
        ))  # fmt: skip

    def _failed(self, key: Any, said: str) -> None:
        result = {"content": [{"type": "text", "text": said}], "isError": True}
        self._to_client(json.dumps({"jsonrpc": "2.0", "id": key, "result": result}))

    def refuse(self, key: Any, said: str) -> None:
        """A call of the session's that its browser will never answer, answered with ``said``."""
        if key is not None and self._calls.give_up(key) is not None:
            self._failed(key, said)

    def second_gone(self) -> None:
        """The second browser's server ended: what waits on it fails now, not at its time-out."""
        for key, waiting in list(self._mine.items()):
            if waiting.which == SECOND and self._mine.pop(key, None) is not None:
                waiting.answer = {"error": {"code": -32000, "message": "the second browser closed"}}
                waiting.done.set()
        for key in self._calls.out(SECOND):
            self.refuse(key, "your second browser closed before it answered")

    def close(self) -> None:
        """Stop the clocks of the calls still out, and the second browser: the server and the session are going."""
        self._calls.stop()
        if self._second is not None:
            self._second.stop()

    def start_trace(self) -> None:
        """Awaited on a thread of its own: the server asks the session for its roots before it starts a trace, and the
        thread that reads the session must stay free to carry the answer back."""
        key, waiting = self._ask("tools/call", {"name": "browser_start_tracing", "arguments": {}}, FIRST)
        self._trace_asked = True
        threading.Thread(target=self._traced, args=(key, waiting), daemon=True).start()

    def _traced(self, key: str, waiting: _Waiting) -> None:
        try:
            answer = self._result(key, waiting, "tools/call")
            reason = text_of(answer)[:400] if answer.get("isError") else ""
        # telemetry: surfaced — `untraced` stops the browser, and `serve` says why on `sys.stderr`
        except ServerError as exc:
            reason = str(exc)
        self._trace_ok = not reason
        if reason:
            self.untraced(reason)
        self._trace_answered.set()

    def traced(self) -> bool:
        """Whether the trace started, once it answered: every call waits for it, so nothing runs untraced. The
        session calls no tool before it has the list, which the server sends only once the trace started."""
        if not self._trace_asked:
            return True
        self._trace_answered.wait(self._wait)
        return self._trace_ok

    def request(self, method: str, params: dict[str, Any], which: str = FIRST) -> dict[str, Any]:
        """Send one request of the wrapper's own and wait for its answer's result; the second browser opens first."""
        if not self.traced():
            raise ServerError("the browser's trace did not start")
        if which == SECOND and self._second is not None:
            self._second.ready()
        return self._result(*self._ask(method, params, which), method)

    def ask_second(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """A request of the wrapper's own to the second browser's server, which may be opening."""
        return self._result(*self._ask(method, params, SECOND), method)

    def _ask(self, method: str, params: dict[str, Any], which: str) -> tuple[str, _Waiting]:
        key = f"{OWN}{next(self._ids)}"
        waiting = _Waiting(which)
        self._mine[key] = waiting
        line = json.dumps({"jsonrpc": "2.0", "id": key, "method": method, "params": params})
        if which == FIRST:
            self._to_server(line)
        elif self._second is not None:
            self._second.write(line)
        else:
            raise ServerError("this browser has no second")
        return key, waiting

    def _result(self, key: str, waiting: _Waiting, method: str) -> dict[str, Any]:
        if not waiting.done.wait(self._wait):
            self._mine.pop(key, None)
            raise ServerError(f"the browser did not answer {method} within {self._wait:.0f} s")
        if "error" in waiting.answer:
            raise ServerError(f"the browser refused {method}: {json.dumps(waiting.answer['error'])[:300]}")
        result = waiting.answer.get("result")
        return result if isinstance(result, dict) else {}

    def call_tool(self, name: str, arguments: dict[str, Any], which: str = FIRST) -> str:
        """One of the server's tools, for a factory tool: its text, or ServerError when it failed."""
        result = self.request("tools/call", {"name": name, "arguments": arguments}, which)
        if result.get("isError"):
            raise ServerError(f"{name} failed: {text_of(result)[:300]}")
        return text_of(result)

    def _factory_tool(self, message: dict[str, Any], cancelled: threading.Event, which: str) -> None:
        params = message.get("params") or {}
        given, name = params.get("arguments"), str(params.get("name"))
        arguments: dict[str, Any] = given if isinstance(given, dict) else {}
        try:
            text, failed = self._answer_tool(name, arguments, cancelled, which)
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


def _cut(content: object, most: int) -> bool:
    """Whether an answer held a text past ``most`` characters, now cut there: Claude Code refuses a tool's answer past
    its own cap, which a snapshot of a page with a recorded page in its frame passes."""
    long = [c for c in content if isinstance(c, dict) and c.get("type") == "text" and len(str(c.get("text")))
            > most] if isinstance(content, list) else []  # fmt: skip
    for part in long:
        part["text"] = str(part["text"])[:most] + CUT
    return bool(long)


def text_of(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text")
