"""`factory qa-browser --check <role>`: whether a tester's browser works where it runs, which the install's drill asks.

It starts the role's browser server with the command a tester's runs, opens a blank page and closes it. It prints one
line, the page opened or what went wrong, and deletes the run.
The server runs in its own process group: npx starts node, which starts the browser, and the check ends all.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from runtime.qa import qa_browser
from runtime.shared import settings as settings_module
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.settings import Settings

FLAG = "--check"
OPENED = "the browser opened a page"
_CALLS: tuple[tuple[str, dict[str, str]], ...] = (("browser_navigate", {"url": "about:blank"}), ("browser_close", {}))
_HELLO = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "factory-check", "version": "1"}}


def _answer(lines: queue.Queue[str], number: int, deadline: float) -> dict[str, Any] | None:
    while (left := deadline - time.monotonic()) > 0:
        try:
            line = lines.get(timeout=left)
        # telemetry: expected — out of time; the check's verdict says so
        except queue.Empty:
            return None
        if not line:
            return None
        try:
            message = json.loads(line)
        # telemetry: expected — a line that is no JSON-RPC message (a log line) is not an answer
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == number:
            return message
    return None


def _exchange(stdin: IO[str], lines: queue.Queue[str], deadline: float) -> str:
    """The handshake and the calls; what went wrong, or "" when every call was answered without an error."""
    requests: list[tuple[str, str, dict[str, Any]]] = [
        ("initialize", "the handshake", _HELLO),
        *(("tools/call", name, {"name": name, "arguments": arguments}) for name, arguments in _CALLS),
    ]
    for number, (method, name, params) in enumerate(requests, 1):
        stdin.write(json.dumps({"jsonrpc": "2.0", "id": number, "method": method, "params": params}) + "\n")
        if number == 1:
            stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        stdin.flush()
        answer = _answer(lines, number, deadline)
        if answer is None:
            return f"the browser server did not answer {name}"
        result = answer.get("result")
        if "error" in answer or not isinstance(result, dict) or result.get("isError"):
            content = result.get("content") if isinstance(result, dict) else None
            said = [c.get("text", "") for c in content if isinstance(c, dict)] if isinstance(content, list) else []
            reason = " ".join(" ".join(said).split()) or json.dumps(answer.get("error", "an answer with no result"))
            return f"{name} failed: {reason[:500]}"
    return ""


def check(role: str, root: Path, settings: Settings, now: datetime, command: Callable[[Path], list[str]]) -> str:
    """Run ``command``, the browser server on a run's settings, through one page; the run is deleted either way."""
    session = str(uuid.uuid4())
    config = qa_browser.prepare(role, session, root, settings, now)
    deadline = time.monotonic() + settings.get_int("install.browser_check_seconds")
    server = subprocess.Popen(command(config), cwd=config.parent, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True, start_new_session=True)  # fmt: skip
    stdin, stdout = server.stdin, server.stdout
    assert stdin is not None and stdout is not None  # both are pipes, as asked
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        for line in stdout:
            lines.put(line)
        lines.put("")

    threading.Thread(target=read, name="browser-check", daemon=True).start()
    try:
        return _verdict(server, stdin, lines, deadline)
    finally:
        # telemetry: expected — the whole group ended with the server once its input closed
        with suppress(ProcessLookupError):
            os.killpg(server.pid, signal.SIGKILL)
        server.wait()
        shutil.rmtree(root / session, ignore_errors=True)


def _verdict(server: subprocess.Popen[str], stdin: IO[str], lines: queue.Queue[str], deadline: float) -> str:
    wrong = _exchange(stdin, lines, deadline)
    stdin.close()  # the server ends with its input, closing the browser
    try:
        server.wait(timeout=max(1.0, deadline - time.monotonic()))
    # telemetry: surfaced — `wrong`, the check's verdict, names the browser server that would not end
    except subprocess.TimeoutExpired:
        wrong = wrong or "the browser server did not end once its input closed"
    return wrong


def main(argv: list[str], places: Places) -> int:
    if len(argv) != 1:
        print(f"factory qa-browser {FLAG} <role>", file=sys.stderr)
        return 2
    try:
        settings = qa_browser.published(places)
        wrong = check(argv[0], Path.home() / qa_browser.RUNS, settings, datetime.now(UTC), qa_browser.server_command)
    # telemetry: surfaced — `wrong` is printed as the check's one line of verdict, which the drill that asked reads
    except (qa_browser.BrowserError, settings_module.SettingsError, store.StoreError, OSError) as exc:
        wrong = f"{type(exc).__name__}: {exc}"
    print(f"no browser: {wrong}" if wrong else OPENED)
    return 1 if wrong else 0
