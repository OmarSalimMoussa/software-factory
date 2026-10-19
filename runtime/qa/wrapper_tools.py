"""The factory's own tools in a tester's browser, answered by the wrapper with Playwright's tools.

- ``browser_sign_in`` signs the browser in as a test address with a fresh one-time link the warden asks the slot
  for. The link never reaches the tester's transcript; it is kept beside the run, so the replay can scrub it.
- ``browser_wait_until`` reloads and reads the page every ``qa.wait_reload_seconds`` until a text shows, up to a
  ceiling, in one call: a tester never polls.
- ``browser_open_desktop`` sets the browser to the desktop's size (``qa.desktop_viewport``), then opens the desktop's
  live view: a screenshot is not shrunk before the tester sees it, so a point in it is the point on the desktop.
- ``browser_color_scheme`` shows the page in its light or dark colours: fixed code the wrapper runs in the browser
  server, whose own code tool is hidden from testers.
- ``browser_mouse_press_xy`` clicks at a point, resting there ``qa.press_settle_ms`` first, holding the button
  ``qa.click_hold_ms``: a remote desktop under load misses a press that comes with its move, or one as short as
  Playwright's.
- ``browser_type_keys`` types a text as key presses into whatever has the keyboard, in one call, ``qa.key_delay_ms``
  apart: the remote desktop's view takes keys, not a field's filled value, drops keys sent faster, and reads a capital
  or a shifted symbol only with Shift held.
- ``browser_hold_request`` holds the next request to an address until ``browser_release_request`` lets it go, or its
  seconds (at most ``qa.hold_max_seconds``) end, then hands it on through the browser's other routes, so a blocked
  address stays blocked: one request can overtake another, at the moment the step needs, whatever a call takes.
"""

from __future__ import annotations

import json
import re
import string
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from runtime.qa.wrapper import SERVER_CODE, WAIT_FOR, ServerError

SIGN_IN, WAIT_UNTIL, OPEN_DESKTOP = "browser_sign_in", "browser_wait_until", "browser_open_desktop"
COLOUR, TYPE_KEYS, HOLD = "browser_color_scheme", "browser_type_keys", "browser_hold_request"
RELEASE, PRESS = "browser_release_request", "browser_mouse_press_xy"
SCHEMES = ("light", "dark")
# Playwright's answers name the page after an action on a line of their own, and its code tool's value after a heading.
PAGE_URL = re.compile(r"Page URL: (\S+)")
RESULT = re.compile(r"### Result\n(.*)\n")
# A link as the sign-in prints it, and as a transcript keeps that output: inside JSON, where a line break is the two
# characters \n, so a backslash ends the link too.
LINK = re.compile(r"https?://[^\s'\"<>)\\]+")
WEB = re.compile(r"^https?://\S+$")
# The key under each character typed with Shift, on the US layout the browser server types with.
SHIFTED = {
    **{c: f"Key{c}" for c in string.ascii_uppercase},
    **dict(
        zip(
            '~!@#$%^&*()_+{}|:"<>?',
            "Backquote Digit1 Digit2 Digit3 Digit4 Digit5 Digit6 Digit7 Digit8 Digit9 "
            "Digit0 Minus Equal BracketLeft BracketRight Backslash Semicolon Quote Comma Period Slash".split(),
            strict=True,
        )
    ),
}

# A line added to one of Playwright's own tools, where a tester reads it as it loads that tool.
NOTES = {
    "browser_type": f"On a remote desktop's live view, type with {TYPE_KEYS} instead: there this tool loses capitals, "
    "symbols and keys sent fast.",
    "browser_mouse_click_xy": f"On a remote desktop's live view, click with {PRESS} instead: a click this short may "
    "not take there.",
    WAIT_FOR: "A text or textGone wait gives up after a few seconds, and time sleeps before either, so a message that "
    f"shows briefly can pass unseen: wait on its text alone, right after the step. {WAIT_UNTIL} waits longer, but "
    "reloads the page.",
}

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
        "name": PRESS,
        "description": "Click at a point on the remote desktop's view, holding the button a moment, as the desktop "
        "takes a click. A point is in the page's pixels from its top-left corner, as a screenshot shows them: a "
        "view inside a frame counts from the page's corner, not the frame's.",
        "inputSchema": {
            "type": "object",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
            "required": ["x", "y"],
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
    {
        "name": HOLD,
        "description": "Hold the next request to an address until you release it, or for at most some seconds, then "
        "send it on as usual, so another request can overtake it. It holds one request, then lifts itself.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "the address, * and ** as wildcards: **/api/runs/*"},
                "seconds": {
                    "type": "number",
                    "description": "the most it waits for your release: allow for every "
                    "call until then, at several seconds each",
                },
            },
            "required": ["pattern", "seconds"],
        },
    },
    {
        "name": RELEASE,
        "description": "Let the request you held to an address go on now, such as right after the request that "
        "overtakes it.",
        "inputSchema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "the address you held, as you gave it"}},
            "required": ["pattern"],
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
    hold_max_seconds: int
    click_hold_ms: int
    press_settle_ms: int
    now: Callable[[], float]  # a monotonic clock, in seconds

    def answer(self, name: str, arguments: dict[str, Any], cancelled: threading.Event) -> tuple[str, bool]:
        """The tool's text, and whether it failed; a wait ends early once ``cancelled`` is set."""
        tools: dict[str, Callable[[dict[str, Any], threading.Event], str]] = {
            SIGN_IN: self._sign_in,
            WAIT_UNTIL: self._wait_until,
            OPEN_DESKTOP: self._open_desktop,
            COLOUR: self._color_scheme,
            TYPE_KEYS: self._type_keys,
            PRESS: self._press,
            HOLD: self._hold_request,
            RELEASE: self._release_request,
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
            if waited >= ceiling:
                # Said at the miss: testers take a wait that missed words in a live view as the step blocked.
                return (
                    f"“{text}” did not show in the page's text within {duration(ceiling)}{capped}. Words drawn in "
                    f"a picture or a live view are no page text: a screenshot shows them. The page now:\n\n{page}"
                )
            # The last look comes at the ceiling, however much shorter than a reload's interval the wait is.
            if cancelled.wait(min(self.reload_seconds, ceiling - waited)):
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
        # The keys go as JSON data to one fixed loop, so the text cannot end the code it sits in.
        keys = json.dumps([["press", f"Shift+{SHIFTED[c]}"] if c in SHIFTED else ["type", c] for c in text])
        each = f"await page.keyboard[how](key); await page.waitForTimeout({self.key_delay_ms});"
        self.call(SERVER_CODE, {"code": f"async (page) => {{ for (const [how, key] of {keys}) {{ {each} }} }}"})
        return (
            f"Typed {len(text)} keys. A desktop may draw them seconds late: see them in a screenshot, then type more."
        )

    def _press(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        x, y = arguments.get("x"), arguments.get("y")
        width, height = self.desktop
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, int | float)
            or not isinstance(y, int | float)
        ):
            raise ToolError("give the point as x and y, in the page's pixels")
        if not (0 <= x < width and 0 <= y < height):
            raise ToolError(f"the point is off the page: x from 0 to {width - 1}, y from 0 to {height - 1}")
        rest = f"await page.mouse.move({x}, {y}); await page.waitForTimeout({self.press_settle_ms});"
        held = f"await page.mouse.down(); await page.waitForTimeout({self.click_hold_ms}); await page.mouse.up();"
        self.call(SERVER_CODE, {"code": f"async (page) => {{ {rest} {held} }}"})
        return f"Pressed at {x:g}, {y:g}. A screenshot shows what it did."

    def _hold_request(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        pattern, seconds = arguments.get("pattern"), arguments.get("seconds")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ToolError("give the address of the request to hold")
        most = self.hold_max_seconds
        if isinstance(seconds, bool) or not isinstance(seconds, int | float) or not 0 < seconds <= most:
            raise ToolError(f"hold it for more than 0 and at most {most} seconds")
        # The code runs where no timer is defined, so the page waits; a release, a later call, finds the hold kept on
        # the browser's context. fallback, never continue: the held request still meets the routes that keep the
        # dashboard out of reach.
        wait = f"await Promise.race([go, page.waitForTimeout({round(seconds * 1000)})]);"
        caught = f"if (hold.state === 'armed') hold.state = 'waiting'; {wait} delete held[p]; await route.fallback();"
        code = (
            "if (held[p]) return 'busy'; const hold = held[p] = { state: 'armed' }; "
            "const go = new Promise((done) => { hold.go = done; }); "
            f"hold.handler = async (route) => {{ {caught} }}; "
            "await page.context().route(p, hold.handler, { times: 1 }); return 'held';"
        )
        if self._held(pattern, code) == "busy":
            raise ToolError(f"a request to {pattern} is held already: release it first")
        return (
            f"The next request to {pattern} waits until you release it, or {seconds:g} s at most: a limit, not time "
            "your step takes. Do the step now, then release it."
        )

    def _release_request(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ToolError("give the address you held, as you gave it")
        was = self._held(pattern, (
            "const hold = held[p]; if (!hold) return null; const was = hold.state; hold.state = 'released'; hold.go(); "
            "if (was === 'armed') { delete held[p]; await page.context().unroute(p, hold.handler); } return was;"
        ))  # fmt: skip
        if was == "waiting":
            return f"The held request to {pattern} goes on now."
        if was == "armed":
            return f"No request to {pattern} had come yet: the hold is lifted."
        raise ToolError(f"no request to {pattern} is held: its hold ended, or none was set")

    def _held(self, pattern: str, body: str) -> Any:
        """``body`` run in the browser server with ``held``, the holds kept on its context, and ``p``, the address."""
        start = f"const held = page.context().__held ??= {{}}; const p = {json.dumps(pattern)}; "
        answer = self.call(SERVER_CODE, {"code": f"async (page) => {{ {start}{body} }}"})
        found = RESULT.search(answer)
        if found is None:
            raise ToolError("the browser answered with no result; report the step blocked with that")
        return json.loads(found.group(1))


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
