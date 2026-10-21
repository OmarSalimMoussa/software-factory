"""The tester's second browser: a browser context of its own in the first browser's process, so a second person signs
in beside the first with their own cookies and storage, at the cost of a page, never of another browser.

It opens on the first call that names it (wrapper.py). The first browser's process is asked for the address it takes
another Playwright server on; a second server connects there and opens its own context, with the first's settings
and blocks, and writes into the run's ``second/`` (qa_browser.py). Its trace starts before any call reaches it, as
the first's does, and lands beside the first's, which the replay tells apart as the one that started first. Calls
sent while it opens wait, in order; when it cannot open, each says why. ``browser_close_second`` closes its context,
then its server, and a later call opens a fresh one; it ends with the first browser at the latest.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from runtime.qa.answers import text_of
from runtime.qa.wrapper import CLOSE, SERVER_CODE, Proxy, ServerError
from runtime.qa.wrapper_tools import RESULT

TOOL = {
    "name": CLOSE,
    "description": "Close your second browser with all it holds: its pages, sign-in, cookies and storage. Close it "
    "once the steps that need it are done; a later call that names it opens a fresh one.",
    "inputSchema": {"type": "object", "properties": {}},
}
# Playwright serves a browser to other servers at one address, opened at its start and told to no one: this opens it
# again to learn it.
REBIND = (
    "async (page) => { const browser = page.context().browser(); await browser.unbind(); "
    "return (await browser.bind('factory-qa-browser')).endpoint; }"
)
# A second server ends its context only when told to: one that just stops leaves it open in the first's process.
CLOSE_CONTEXT = {"name": "browser_close", "arguments": {}}


@dataclass
class _Opening:
    done: threading.Event = field(default_factory=threading.Event)
    error: str = ""


class Second:
    def __init__(self, proxy: Proxy, command: Callable[[str], tuple[list[str], Path]], wait: float) -> None:
        self._proxy, self._command, self._wait = proxy, command, wait
        self._lock, self._writing = threading.Lock(), threading.Lock()
        self._server: subprocess.Popen[str] | None = None
        self._pump: threading.Thread | None = None
        self._opening: _Opening | None = None
        self._queue: list[str] = []  # the session's lines that came while it opened

    def send(self, line: str) -> None:
        with self._lock:
            if self._opening is None and self._server is not None:
                self._pass(line)
                return
            self._queue.append(line)
            self._begin()

    def _pass(self, line: str) -> None:
        try:
            self.write(line)
        # telemetry: surfaced — `refuse` answers the call with why, as a browser's own failure is
        except ServerError as exc:
            self._proxy.refuse(json.loads(line).get("id"), str(exc))

    def write(self, line: str) -> None:
        server = self._server
        if server is None or server.stdin is None:
            raise ServerError("your second browser is closed")
        try:
            with self._writing:
                server.stdin.write(line.rstrip("\n") + "\n")
                server.stdin.flush()
        except OSError as exc:
            raise ServerError(f"your second browser stopped ({type(exc).__name__})") from exc

    def ready(self) -> None:
        with self._lock:
            if self._opening is None and self._server is not None:
                return
            opening = self._begin()
        opening.done.wait()
        if opening.error:
            raise ServerError(opening.error)

    def _begin(self) -> _Opening:
        """The open under way, begun if none is; the caller holds the lock."""
        if self._opening is None:
            self._opening = _Opening()
            threading.Thread(target=self._open, args=(self._opening,), daemon=True).start()
        return self._opening

    def _open(self, opening: _Opening) -> None:
        try:
            self._start()
        # telemetry: surfaced — `opening.error` answers every call that waited for it with why it did not open
        except (ServerError, OSError, ValueError) as exc:
            opening.error = f"your second browser did not open: {exc}; report the step blocked with that"
        with self._lock:
            queued, self._queue, self._opening = self._queue, [], None
            ended = self._detach() if opening.error else (None, None)
            for line in queued:
                if opening.error:
                    self._proxy.refuse(json.loads(line).get("id"), opening.error)
                else:
                    self._pass(line)
        self._end(*ended)
        opening.done.set()

    def _start(self) -> None:
        found = RESULT.search(self._proxy.call_tool(SERVER_CODE, {"code": REBIND}))
        if found is None:
            raise ServerError("the first browser named no address to connect to")
        argv, cwd = self._command(str(json.loads(found.group(1))))
        server = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        pump = threading.Thread(target=self._heard, args=(server,), daemon=True)
        with self._lock:
            self._server, self._pump = server, pump
        pump.start()
        self._proxy.ask_second("initialize", {**self._proxy.hello, "capabilities": {}})
        self.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        answer = self._proxy.ask_second("tools/call", {"name": "browser_start_tracing", "arguments": {}})
        if answer.get("isError"):
            raise ServerError(f"its trace did not start: {text_of(answer)[:300]}")

    def _heard(self, server: subprocess.Popen[str]) -> None:
        """Its server's lines, until it ends: a question it asks its client, the wrapper, is answered here."""
        assert server.stdout is not None  # asked for as a pipe
        for line in server.stdout:
            message = json.loads(line) if line.strip() else {}
            if "method" in message and "id" in message:
                asked = message["method"] == "roots/list"
                said = {"result": {"roots": []}} if asked else {"error": {"code": -32601, "message": "not answered"}}
                try:
                    self.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], **said}))
                # telemetry: expected — a server gone before its answer has nothing left to ask
                except ServerError:
                    break
            elif message:
                self._proxy.from_server(line)
        self._proxy.second_gone()

    def close(self) -> tuple[str, bool]:
        with self._lock:
            if self._server is None and self._opening is None:
                return "You have no second browser open.", False
        try:
            self.ready()
            self._proxy.ask_second("tools/call", CLOSE_CONTEXT)
        # telemetry: surfaced — the tester reads `exc` in the close's answer, and the server stops all the same
        except ServerError as exc:
            self.stop()
            return f"your second browser stopped, though not as asked: {exc}", True
        self.stop()
        return (
            "Your second browser closed, with its sign-in, cookies and storage. A call that names it opens anew.",
            False,
        )

    def stop(self) -> None:
        with self._lock:
            ended = self._detach()
        self._end(*ended)

    def _detach(self) -> tuple[subprocess.Popen[str] | None, threading.Thread | None]:
        """Its server and the thread that reads it, handed to the caller to end; the caller holds the lock."""
        ended, self._server, self._pump = (self._server, self._pump), None, None
        return ended

    def _end(self, server: subprocess.Popen[str] | None, pump: threading.Thread | None) -> None:
        """A server that ends once its input closes, or is ended; its reader is done before this returns."""
        if server is None:
            return
        try:
            if server.stdin is not None:
                server.stdin.close()
            server.wait(self._wait)
        # telemetry: expected — a server that cannot be told, or does not end when told, is ended here
        except (OSError, subprocess.TimeoutExpired):
            server.kill()
            server.wait()
        if pump is not None:
            pump.join(self._wait)
