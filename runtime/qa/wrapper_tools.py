"""The factory's own tools in a tester's browser, answered by the wrapper with Playwright's tools.

- ``browser_sign_in`` signs the browser in as a test address with a fresh one-time link the warden asks the slot
  for. The link never reaches the tester's transcript; it is kept beside the run, so the replay can scrub it.
- ``browser_wait_until`` reloads and reads the page every ``qa.wait_reload_seconds`` until a text shows, up to a
  ceiling, in one call: a tester never polls.
- ``browser_open_desktop`` sets the browser to the desktop's size (``qa.desktop_viewport``), then opens the desktop's
  live view: a screenshot is not shrunk before the tester sees it, so a point in it is the point on the desktop.
- ``browser_color_scheme`` shows the page in its light or dark colours: fixed code the wrapper runs in the browser
  server, whose own code tool is hidden from testers.
- ``browser_type_keys`` types a text as key presses into whatever has the keyboard, in one call, ``qa.key_delay_ms``
  apart: the remote desktop's view takes keys, not a field's filled value, and drops keys sent faster.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from runtime.qa.wrapper import SERVER_CODE, ServerError

SIGN_IN, WAIT_UNTIL, OPEN_DESKTOP = "browser_sign_in", "browser_wait_until", "browser_open_desktop"
COLOUR, TYPE_KEYS = "browser_color_scheme", "browser_type_keys"
SCHEMES = ("light", "dark")
# Playwright's answers name the page after an action on a line of their own.
PAGE_URL = re.compile(r"Page URL: (\S+)")
# A link as the sign-in prints it, and as a transcript keeps that output: inside JSON, where a line break is the two
# characters \n, so a backslash ends the link too.
LINK = re.compile(r"https?://[^\s'\"<>)\\]+")
WEB = re.compile(r"^https?://\S+$")

TOOLS: list[dict[str, Any]] = [
    {
        "name": SIGN_IN,
        "description": "Sign this browser in as a test address with a fresh one-time link from your slot. The link "
        "is never shown to you.",
        "inputSchema": {
            "type": "object",
            "properties": {"address": {"type": "string", "description": "the test address your brief names"}},
            "required": ["address"],
        },
    },
    {
        "name": WAIT_UNTIL,
        "description": "Wait until a text shows on the page, reloading the page every so often, up to a ceiling. One "
        "call does the whole wait. It reads the page's own text, never words drawn in a picture or a live remote "
        "view: check those with a screenshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "the words to wait for, as the page shows them"},
                "minutes": {"type": "number", "description": "the ceiling, in minutes"},
            },
            "required": ["text", "minutes"],
        },
    },
    {
        "name": OPEN_DESKTOP,
        "description": "Open your slot's remote desktop view at the desktop's own size, so your clicks land where you "
        "aim.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "the view's address `factory slot desktop up` " "printed"}
            },
            "required": ["url"],
        },
    },
    {
        "name": COLOUR,
        "description": "Show this page in its light or dark colours, as a person's system setting would. Your "
        "browser starts light; a switch holds across reloads until you switch back.",
        "inputSchema": {
            "type": "object",
            "properties": {"scheme": {"type": "string", "enum": list(SCHEMES)}},
            "required": ["scheme"],
        },
    },
    {
        "name": TYPE_KEYS,
        "description": "Type a text as key presses into whatever has the keyboard, such as the remote desktop's view "
        "after you click where the text goes. One call types it all.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "the text to type"}},
            "required": ["text"],
        },
    },
]


class ToolError(Exception):
    """A factory tool cannot do what was asked; the message, which the tester reads, says why."""


@dataclass
class Tools:
    call: Callable[[str, dict[str, Any]], str]  # one of the server's tools: its text
    link_for: Callable[[str], str]  # a fresh one-time sign-in link for a test address
    keep_secret: Callable[[str], None]  # notes a secret beside the run, for the replay to scrub
    reload_seconds: int
    max_minutes: int
    desktop: tuple[int, int]
    key_delay_ms: int
    now: Callable[[], float]  # a monotonic clock, in seconds

    def answer(self, name: str, arguments: dict[str, Any], cancelled: threading.Event) -> tuple[str, bool]:
        """The tool's text, and whether it failed; a wait ends early once ``cancelled`` is set."""
        tools: dict[str, Callable[[dict[str, Any], threading.Event], str]] = {
            SIGN_IN: self._sign_in,
            WAIT_UNTIL: self._wait_until,
            OPEN_DESKTOP: self._open_desktop,
            COLOUR: self._color_scheme,
            TYPE_KEYS: self._type_keys,
        }
        try:
            return tools[name](arguments, cancelled), False
        # telemetry: surfaced — the tester reads `str(exc)` as the tool's answer, as it reads any browser tool's failure
        except (ToolError, ServerError) as exc:
            return str(exc), True

    def _sign_in(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        address = arguments.get("address")
        if not isinstance(address, str) or "@" not in address:
            raise ToolError("name the test address your brief gives, such as tester-1@example.com")
        link = self.link_for(address)
        self.keep_secret(link)
        try:
            page = self.call("browser_navigate", {"url": link})
        except ServerError as exc:
            raise ToolError(str(exc).replace(link, "the sign-in link")) from exc
        return f"Signed in as {address}.\n\n" + page.replace(link, "the sign-in link")

    def _wait_until(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        text, minutes = arguments.get("text"), arguments.get("minutes")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("name the words to wait for")
        if not isinstance(minutes, int | float) or minutes <= 0:
            raise ToolError("give the ceiling in minutes, above zero")
        ceiling = min(float(minutes), float(self.max_minutes)) * 60
        capped = f" (the most one wait may last is {self.max_minutes} min)" if minutes > self.max_minutes else ""
        start = self.now()
        while True:
            page = self.call("browser_snapshot", {})
            waited = self.now() - start
            if text in page:
                return f"“{text}” shows, after {duration(waited)}.\n\n{page}"
            if waited + self.reload_seconds > ceiling:
                # Said at the miss: testers take a wait that missed words in a live view as the step blocked.
                return (
                    f"“{text}” did not show in the page's text within {duration(ceiling)}{capped}. Words drawn in "
                    f"a picture or a live view are no page text: a screenshot shows them. The page now:\n\n{page}"
                )
            if cancelled.wait(self.reload_seconds):
                raise ToolError("the wait was cancelled")
            found = PAGE_URL.search(page)
            if found is None:
                raise ToolError("the page names no address to reload, so the wait stopped")
            self.call("browser_navigate", {"url": found.group(1)})

    def _open_desktop(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        url = arguments.get("url")
        if not isinstance(url, str) or not WEB.match(url):
            raise ToolError("give the view's address, starting http:// or https://")
        width, height = self.desktop
        self.call("browser_resize", {"width": width, "height": height})
        return self.call("browser_navigate", {"url": url})

    def _color_scheme(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        scheme = arguments.get("scheme")
        if scheme not in SCHEMES:
            raise ToolError("say light or dark")
        self.call(
            SERVER_CODE, {"code": f"async (page) => {{ await page.emulateMedia({{ colorScheme: '{scheme}' }}); }}"}
        )
        return f"The page now shows its {scheme} colours, and keeps them across reloads. Your browser started light."

    def _type_keys(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        text = arguments.get("text")
        if not isinstance(text, str) or not text:
            raise ToolError("give the text to type")
        # json.dumps makes the text one JavaScript string, so it cannot end the code it sits in.
        typed = f"await page.keyboard.type({json.dumps(text)}, {{ delay: {self.key_delay_ms} }});"
        self.call(SERVER_CODE, {"code": f"async (page) => {{ {typed} }}"})
        return f"Typed {len(text)} keys. A screenshot shows where they landed."


def first_link(text: str) -> str:
    """The one-time link in what the product's sign-in printed; ToolError when it printed none."""
    found = LINK.search(text)
    if found is None:
        raise ToolError("your slot's sign-in printed no link; report the step blocked with that")
    return found.group(0)


def duration(seconds: float) -> str:
    whole = round(seconds)
    minutes, rest = divmod(whole, 60)
    if not minutes:
        return f"{rest} s"
    return f"{minutes} min" + (f" {rest} s" if rest else "")
