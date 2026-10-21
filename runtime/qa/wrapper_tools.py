"""The factory's own tools in a tester's browser, answered by the wrapper with Playwright's tools.

- ``browser_sign_in`` signs the browser in as a test address with a fresh one-time link the warden asks the slot
  for. The link never reaches the tester's transcript; it is kept beside the run, so the replay can scrub it.
- ``browser_wait_until`` reads the page until a text shows, up to a ceiling, in one call, so a tester never polls:
  with ``reload``, a reload every ``qa.wait_reload_seconds`` before each look; without, the page is left alone.
- ``browser_wait_for`` with a time alone waits here, not in the browser, whose wait needs an open page.
- ``browser_open_desktop`` sets the browser to the desktop's size (``qa.desktop_viewport``), then opens the desktop's
  live view: a screenshot is not shrunk before the tester sees it, so a point in it is the point on the desktop.
- ``browser_color_scheme`` shows the page light or dark: fixed code the wrapper runs, as testers' code tool is hidden.
- ``browser_mouse_press_xy`` clicks at a point as a remote desktop under load takes a click, then answers once the
  desktop's live view shows a change, or says it saw none: the desktop draws a click's effect seconds late.
- ``browser_type_keys`` types a text as key presses into whatever has the keyboard, in one call, ``qa.key_delay_ms``
  apart: the remote desktop's view takes keys, not a field's filled value, drops keys sent faster, and reads a capital
  or a shifted symbol only with Shift held.
- ``browser_hold_request`` holds the next request to an address until ``browser_release_request`` lets it go, or its
  seconds (at most ``qa.hold_max_seconds``) end, then hands it on through the browser's other routes, so a blocked
  address stays blocked: one request can overtake another, at the moment the step needs, whatever a call takes.
- ``browser_fail_request`` fails the next request to an address once with a network error, then lifts itself.
"""

from __future__ import annotations

import json
import re
import string
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from runtime.qa.answers import PAGE_URL
from runtime.qa.tool_list import (
    COLOUR,
    ERRORS,
    FAIL,
    HOLD,
    OPEN_DESKTOP,
    PRESS,
    RELEASE,
    SCHEMES,
    SIGN_IN,
    TYPE_KEYS,
    WAIT_UNTIL,
)
from runtime.qa.wrapper import SERVER_CODE, WAIT_FOR, ServerError

# Playwright's code tool names its value after a heading.
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


# The live view's picture: the largest video playing in a frame, drawn into a canvas of a few cells, each the average
# grey of the pixels it covers (a canvas shrinks a video by averaging them; its data holds four bytes a pixel). null
# with no video playing, false when the canvas will not give its pixels back, as for a video from another site.
PICTURE = (
    "([w, h]) => { const shown = [...document.querySelectorAll('video')].filter((v) => v.videoWidth > 0 && "
    "v.readyState >= v.HAVE_CURRENT_DATA); if (!shown.length) return null; const video = shown.reduce((a, b) => "
    "b.clientWidth * b.clientHeight > a.clientWidth * a.clientHeight ? b : a); const pen = Object.assign("
    "document.createElement('canvas'), { width: w, height: h }).getContext('2d', { willReadFrequently: true }); "
    "pen.drawImage(video, 0, 0, w, h); try { const d = pen.getImageData(0, 0, w, h).data; "
    "return Array.from({ length: w * h }, (_, i) => (d[4 * i] + d[4 * i + 1] + d[4 * i + 2]) / 3); } "
    "catch { return false; } }"
)


@dataclass(frozen=True, slots=True)
class Desktop:
    """How the factory's tools work a remote desktop's live view: the ``qa.`` settings of these names."""

    viewport: tuple[int, int]  # qa.desktop_viewport
    key_delay_ms: int
    click_hold_ms: int
    press_settle_ms: int
    press_wait_ms: int
    press_look_ms: int
    press_look_cells: tuple[int, int]
    press_look_level: int


class ToolError(Exception):
    """A factory tool cannot do what was asked; the message, which the tester reads, says why."""


def _wait_asked(arguments: dict[str, Any]) -> tuple[str, float, bool]:
    text, minutes, reload = arguments.get("text"), arguments.get("minutes"), arguments.get("reload")
    if not isinstance(text, str) or not text.strip():
        raise ToolError("name the words to wait for")
    if not isinstance(minutes, int | float) or minutes <= 0:
        raise ToolError("give the ceiling in minutes, above zero")
    if not isinstance(reload, bool):
        raise ToolError("say whether to reload the page between looks: true or false")
    return text, float(minutes), reload


@dataclass
class Tools:
    call: Callable[[str, dict[str, Any]], str]  # one of the server's tools: its text
    link_for: Callable[[str], str]  # a fresh one-time sign-in link for a test address
    keep_secret: Callable[[str], None]  # notes a secret beside the run, for the replay to scrub
    signed_in: Callable[[str], None]
    reload_seconds: int
    max_minutes: int
    desktop: Desktop
    hold_max_seconds: int
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
            FAIL: self._fail_request,
            RELEASE: self._release_request,
            WAIT_FOR: self._wait_seconds,
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
        self.signed_in(address)
        return f"Signed in as {address}.\n\n" + page.replace(link, "the sign-in link")

    def _wait_until(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        text, minutes, reload = _wait_asked(arguments)
        ceiling = min(float(minutes), float(self.max_minutes)) * 60
        capped = f" (the most one wait may last is {self.max_minutes} min)" if minutes > self.max_minutes else ""
        start = self.now()
        while True:
            page = self.call("browser_snapshot", {}) if reload else self._live_look(text)
            waited = self.now() - start
            if text in page:
                return f"“{text}” shows, after {duration(waited)}.\n\n{page}"
            if waited >= ceiling:
                # Said at the miss: testers take a wait that missed words in a live view as the step blocked.
                return (
                    f"“{text}” did not show in the page's text within {duration(ceiling)}{capped}. Words drawn in "
                    f"a picture or a live view are no page text: a screenshot shows them. The page now:\n\n{page}"
                )
            if cancelled.is_set():
                raise ToolError("the wait was cancelled")
            if not reload:
                continue
            found = PAGE_URL.search(page)
            if found is None:
                raise ToolError("the page names no address to reload, so the wait stopped")
            # A reload, then the wait: each look comes once the page has loaded, and the last at the ceiling.
            self.call("browser_navigate", {"url": found.group(1)})
            if cancelled.wait(min(self.reload_seconds, ceiling - waited)):
                raise ToolError("the wait was cancelled")

    def _live_look(self, text: str) -> str:
        """The page once ``text`` shows, or as it is once Playwright's own short wait for it ends."""
        try:
            return self.call(WAIT_FOR, {"text": text})
        # telemetry: expected — the words have not shown yet; a browser that failed fails the snapshot loudly
        except ServerError:
            return self.call("browser_snapshot", {})

    def _wait_seconds(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        seconds = arguments.get("time")
        if isinstance(seconds, bool) or not isinstance(seconds, int | float) or seconds < 0:
            raise ToolError("give the wait's time in seconds")
        if cancelled.wait(seconds):
            raise ToolError("the wait was cancelled")
        return f"Waited {seconds:g} s."

    def _open_desktop(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        url = arguments.get("url")
        if not isinstance(url, str) or not WEB.match(url):
            raise ToolError("give the view's address, starting http:// or https://")
        width, height = self.desktop.viewport
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
        each = f"await page.keyboard[how](key); await page.waitForTimeout({self.desktop.key_delay_ms});"
        self.call(SERVER_CODE, {"code": f"async (page) => {{ for (const [how, key] of {keys}) {{ {each} }} }}"})
        return (
            f"Typed {len(text)} keys. A desktop may draw them seconds late: see them in a screenshot, then type more."
        )

    def _press(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        x, y = arguments.get("x"), arguments.get("y")
        width, height = self.desktop.viewport
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, int | float)
            or not isinstance(y, int | float)
        ):
            raise ToolError("give the point as x and y, in the page's pixels")
        if not (0 <= x < width and 0 <= y < height):
            raise ToolError(f"the point is off the page: x from 0 to {width - 1}, y from 0 to {height - 1}")
        seen = self._value(_press_code(x, y, self.desktop))
        pressed, most = f"Pressed at {x:g}, {y:g}.", f"{self.desktop.press_wait_ms / 1000:g} s"
        said = {
            "none": f"{pressed} A screenshot shows what it did.",
            "same": f"{pressed} No change on the desktop within {most}: take a screenshot before you click again.",
            "unread": f"{pressed} The factory cannot read this view's picture, so it waited {most}: take a "
            "screenshot before you click again.",
        }
        if isinstance(seen, str):
            return said[seen]
        return f"{pressed} The desktop changed {seen / 1000:.1f} s after the press: a screenshot shows what it did."

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

    def _fail_request(self, arguments: dict[str, Any], cancelled: threading.Event) -> str:
        pattern, error = arguments.get("pattern"), arguments.get("error")
        if not isinstance(pattern, str) or not pattern.strip():
            raise ToolError("give the address of the request to fail")
        if error not in ERRORS:
            raise ToolError(f"name the network error: one of {', '.join(ERRORS)}")
        was = self._held(pattern, (
            "const failing = page.context().__failing ??= {}; const was = failing[p]; "
            "if (was) await page.context().unroute(p, was); const fail = failing[p] = async (route) => { "
            f"delete failing[p]; await route.abort({json.dumps(error)}); }}; "
            "await page.context().route(p, fail, { times: 1 }); return !!was;"
        ))  # fmt: skip
        again = " It replaces the failure set before." if was else ""
        return f"The next request to {pattern} fails with {error}, once; the one after goes through.{again}"

    def _held(self, pattern: str, body: str) -> Any:
        """``body`` run in the browser server with ``held``, the holds kept on its context, and ``p``, the address."""
        start = f"const held = page.context().__held ??= {{}}; const p = {json.dumps(pattern)}; "
        return self._value(f"async (page) => {{ {start}{body} }}")

    def _value(self, code: str) -> Any:
        """What ``code``, run in the browser server, returned."""
        answer = self.call(SERVER_CODE, {"code": code})
        found = RESULT.search(answer)
        if found is None:
            raise ToolError("the browser answered with no result; report the step blocked with that")
        return json.loads(found.group(1))


def _press_code(x: float, y: float, desk: Desktop) -> str:
    """A click as code for the browser server, which answers ``none`` with no live view on the page, ``unread`` once
    it waited on a view whose picture it cannot read, ``same`` when no look saw a change, or the milliseconds from the
    release to the look that saw one."""
    look = (
        "const look = async () => { for (const frame of page.frames()) { const seen = await frame.evaluate("
        f"{PICTURE}, {json.dumps(list(desk.press_look_cells))}).catch(() => null); if (seen !== null) return seen; "
        "} return null; };"
    )
    # A press that comes with its move, or a shorter one, misses on a desktop under load.
    press = (
        f"await page.mouse.move({x}, {y}); await page.waitForTimeout({desk.press_settle_ms}); "
        "const before = await look(); "
        f"await page.mouse.down(); await page.waitForTimeout({desk.click_hold_ms}); await page.mouse.up();"
    )
    wait, level = desk.press_wait_ms, desk.press_look_level
    # A picture gone after the press is a change too: the page left the view.
    watch = (
        f"if (before === null) return 'none'; if (before === false) {{ await page.waitForTimeout({wait}); "
        f"return 'unread'; }} const start = Date.now(); while (Date.now() - start < {wait}) {{ "
        f"await page.waitForTimeout({desk.press_look_ms}); const now = await look(); "
        f"if (!now || now.some((v, i) => Math.abs(v - before[i]) > {level})) return Date.now() - start; }} "
        "return 'same';"
    )
    return f"async (page) => {{ {look} {press} {watch} }}"


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
