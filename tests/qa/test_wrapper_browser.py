"""The wrapper in front of Playwright's own MCP server and headless Chrome, as a tester's browser runs; skipped where
node, Chrome or the pinned server is missing."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
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
        served = (argv, lambda proxy: None, WAIT, 100_000, client_in, client_out)
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
