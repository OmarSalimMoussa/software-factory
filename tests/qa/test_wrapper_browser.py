"""The wrapper in front of Playwright's own MCP server and headless Chrome, as a tester's browser runs; skipped where
node, Chrome or the pinned server is missing."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from queue_kit import World

from runtime.qa import qa_browser

CHROME = Path(os.environ.get("CHROME", "/usr/bin/google-chrome"))
VERSION = qa_browser.PLAYWRIGHT_MCP.rpartition("@")[2]
PAGE = 'data:text/html,<div class="ext-badge" data-tone="ok">badge</div><button>Account</button>'
WAIT = 30


def _server() -> Path | None:
    base = Path.home() / ".npm/_npx"
    found = sorted(base.glob("*/node_modules/@playwright/mcp/package.json")) if base.is_dir() else []
    return next((p.with_name("cli.js") for p in found if json.loads(p.read_text())["version"] == VERSION), None)


class Browser:
    def __init__(self, tmp_path: Path, world: World, cli: Path) -> None:
        settings = world.ctx().settings()
        config = tmp_path / "playwright.json"
        config.write_text(json.dumps(qa_browser.playwright_settings(tmp_path, "qa-ui-function", settings)))
        argv = ["node", str(cli), *qa_browser.server_command(config)[3:]]
        read_in, write_in = os.pipe()
        read_out, write_out = os.pipe()
        self.to_wrapper, self.from_wrapper = os.fdopen(write_in, "w"), os.fdopen(read_out)
        client_in, client_out = os.fdopen(read_in), os.fdopen(write_out, "w")
        second = qa_browser.second_command(tmp_path, "qa-ui-function", settings)
        equip = qa_browser.equipper(lambda address: "", lambda secret: None, settings, second)
        served = (argv, equip, WAIT, 100_000, client_in, client_out)
        self.thread = threading.Thread(target=qa_browser.serve, args=served)
        self.thread.start()
        self.sent = 0
        self.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _write(self, message: dict[str, Any]) -> None:
        self.to_wrapper.write(json.dumps(message) + "\n")
        self.to_wrapper.flush()

    def send(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.sent += 1
        self._write({"jsonrpc": "2.0", "id": self.sent, "method": method, "params": params})
        while True:
            answer: dict[str, Any] = json.loads(self.from_wrapper.readline())
            if answer.get("id") == self.sent:
                return answer

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        result = self.send("tools/call", {"name": name, "arguments": arguments})["result"]
        return result["content"][0]["text"], bool(result.get("isError"))

    def evaluate(self, script: str) -> tuple[str, bool, float]:
        started = time.monotonic()
        said, failed = self.call("browser_evaluate", {"function": script})
        return said, failed, time.monotonic() - started

    def close(self) -> None:
        self.to_wrapper.close()
        self.thread.join(WAIT)


@pytest.mark.skipif(not (shutil.which("node") and CHROME.is_file() and _server()), reason="needs node, Chrome, MCP")
def test_a_page_script_whose_callback_throws_ends_at_once_naming_the_error_and_others_answer_as_written(
    tmp_path: Path, world: World
) -> None:
    cli = _server()
    assert cli is not None
    browser = Browser(tmp_path, world, cli)
    try:
        browser.send("tools/call", {"name": "browser_navigate", "arguments": {"url": PAGE}})
        # A tester's own script: `:has-text` is Playwright's, so the page's querySelector threw inside the callback.
        script = ("() => new Promise(r => setTimeout(() => { const b = document.querySelector(\".ext-badge\"); "
                  "r({ tone: b.dataset.tone, accountH: document.querySelector('button:has-text, button') ? null : null "
                  "}); }, 600))")  # fmt: skip
        said, failed, took = browser.evaluate(script)
        assert failed and "'button:has-text, button' is not a valid selector" in said and took < WAIT / 2

        said, failed, _ = browser.evaluate("() => document.querySelector('.ext-badge').dataset.tone")
        assert not failed and '"ok"' in said
        assert "await page.evaluate('() => document.querySelector(\\'.ext-badge\\').dataset.tone');" in said
    finally:
        browser.close()


# A live list: draw() makes every row anew, as a page that polls does, so the refs of an earlier snapshot go stale.
LIST = "data:text/html," + quote(
    "<p id=shown>none</p><ul id=rows></ul><script>function draw() { rows.innerHTML = ['a', 'b'].map((w) => "
    "`<li>${w} <button onclick=\"shown.textContent='${w}'\">Invite ${w}</button> <button>Remove</button></li>`)"
    ".join('') + '<input aria-label=Note>'; } draw();</script>"
)
STALE = "### Error\nError: Ref <ref> not found in the current page snapshot. Try capturing new snapshot."
FILL = "browser_fill_form"


@pytest.mark.skipif(not (shutil.which("node") and CHROME.is_file() and _server()), reason="needs node, Chrome, MCP")
@pytest.mark.parametrize(
    ("element", "tool", "says", "shows"),
    [
        ('button "Invite a"', "browser_click", r'The page drew button "Invite a" anew, so ref <ref> is e\d+ now: '
         r"the factory sent your call again with it\.\n\n### Ran Playwright code", '"a"'),
        ('button "Remove"', "browser_click", re.escape(STALE) + "\nThe page changed since your snapshot: 2 elements "
         'are button "Remove" now. Pick one in a snapshot.$', '"none"'),
        ('textbox "Note"', FILL, re.escape(STALE) + "$", '"none"'),
        ("unseen", "browser_click", re.escape(STALE) + "$", '"none"'),
    ],
    ids=["one-like-it", "several-like-it", "may-have-acted", "never-seen"],
)  # fmt: skip
def test_a_call_to_an_element_the_page_drew_anew_goes_again_once_to_the_one_element_like_it(
    tmp_path: Path, world: World, element: str, tool: str, says: str, shows: str
) -> None:
    cli = _server()
    assert cli is not None
    browser = Browser(tmp_path, world, cli)
    try:
        browser.call("browser_navigate", {"url": LIST})
        seen = re.search(rf"{re.escape(element)} \[ref=(e\d+)\]", browser.call("browser_snapshot", {})[0])
        ref = seen.group(1) if seen else "e99"
        browser.evaluate("() => draw()")
        target = {"fields": [{"target": ref, "name": "Note", "type": "textbox", "value": "x"}]} if tool == FILL else {}
        said, failed = browser.call(tool, target or {"target": ref})
        page = browser.evaluate("() => shown.textContent")[0]
    finally:
        browser.close()
    assert re.match(says.replace("<ref>", ref), said) and failed == (shows == '"none"')
    assert f"### Result\n{shows}\n" in page


class _Site(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"pong" if self.path == "/ping" else b"<p>site</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.mark.skipif(not (shutil.which("node") and CHROME.is_file() and _server()), reason="needs node, Chrome, MCP")
def test_a_request_set_to_fail_fails_once_with_its_network_error_and_the_next_goes_through(
    tmp_path: Path, world: World
) -> None:
    cli = _server()
    assert cli is not None
    site = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    browser = Browser(tmp_path, world, cli)
    fetch = "() => fetch('/ping').then((r) => r.text(), (e) => `failed: ${e.message}`)"
    try:
        browser.call("browser_navigate", {"url": f"http://127.0.0.1:{site.server_port}/"})
        armed = [
            browser.call("browser_fail_request", {"pattern": "**/ping", "error": e})
            for e in ("timedout", "connectionreset")
        ]
        got = [browser.evaluate(fetch)[0] for _ in "ab"]
        console = browser.call("browser_console_messages", {})[0]
    finally:
        browser.close()
        site.shutdown()
    assert armed == [
        ("The next request to **/ping fails with timedout, once; the one after goes through.", False),
        ("The next request to **/ping fails with connectionreset, once; the one after goes through. It replaces the "
         "failure set before.", False),
    ]  # fmt: skip
    assert '"failed: Failed to fetch"' in got[0] and '"pong"' in got[1]
    assert "ERR_CONNECTION_RESET" in console and "ERR_TIMED_OUT" not in console


def _live_view(late: int | None) -> str:
    """A live view as a remote desktop's: a video of a white picture, sent over WebRTC from one connection in the page
    to another; a press turns the picture black ``late`` ms after it, or never."""
    press = f"addEventListener('mousedown', () => setTimeout(() => {{ shade = 0; }}, {late})); " if late else ""
    return "data:text/html," + quote(
        "<canvas id=src width=320 height=200 hidden></canvas><video id=v autoplay muted playsinline "
        "style='position:fixed;inset:0;width:100%;height:100%'></video><script>const pen = src.getContext('2d'); "
        "let shade = 255; setInterval(() => { pen.fillStyle = `rgb(${shade},${shade},${shade})`; "
        f"pen.fillRect(0, 0, 320, 200); }}, 50); {press}"
        "const a = new RTCPeerConnection(), b = new RTCPeerConnection(); "
        "a.onicecandidate = (e) => e.candidate && b.addIceCandidate(e.candidate); "
        "b.onicecandidate = (e) => e.candidate && a.addIceCandidate(e.candidate); "
        "b.ontrack = (e) => { v.srcObject = e.streams[0]; }; "
        "src.captureStream().getTracks().forEach((t) => a.addTrack(t, new MediaStream([t]))); "
        "(async () => { await a.setLocalDescription(); await b.setRemoteDescription(a.localDescription); "
        "await b.setLocalDescription(); await a.setRemoteDescription(b.localDescription); })();</script>"
    )


@pytest.mark.skipif(not (shutil.which("node") and CHROME.is_file() and _server()), reason="needs node, Chrome, MCP")
@pytest.mark.parametrize(
    ("page", "says"),
    [
        (_live_view(1000), r"The desktop changed (\d\.\d) s after the press: a screenshot shows what it did\."),
        (_live_view(None), r"No change on the desktop within 6 s: take a screenshot before you click again\."),
        (PAGE, r"A screenshot shows what it did\."),
    ],
    ids=["the-picture-changes-late", "the-picture-stays", "no-live-view"],
)
def test_a_desktop_click_reads_the_live_views_video_and_answers_once_it_changed_or_its_wait_ended(
    tmp_path: Path, world: World, page: str, says: str
) -> None:
    cli = _server()
    assert cli is not None
    browser = Browser(tmp_path, world, cli)
    playing = (
        "() => new Promise((done) => { const t = setInterval(() => { const v = document.querySelector('video'); "
        "if (!v || v.readyState >= v.HAVE_CURRENT_DATA) { clearInterval(t); done(!!v); } }, 50); })"
    )
    try:
        browser.call("browser_navigate", {"url": page})
        shown = browser.evaluate(playing)[0]
        said, failed = browser.call("browser_mouse_press_xy", {"x": 640, "y": 400})
    finally:
        browser.close()
    assert ("### Result\ntrue" in shown) == (page != PAGE)
    found = re.fullmatch(r"Pressed at 640, 400\. " + says, said)
    assert found and not failed, said
    assert not found.groups() or 0.8 <= float(found.group(1)) < 6, "the picture turned black 1 s after the press"
