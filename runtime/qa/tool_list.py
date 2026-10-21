"""The factory's own tools as a tester's browser lists them, beside Playwright's (wrapper_tools.py answers them):
each one's name and what it says it does, and the line added to some of Playwright's own tools.
"""

from __future__ import annotations

from typing import Any

from runtime.qa.wrapper import WAIT_FOR

SIGN_IN, WAIT_UNTIL, OPEN_DESKTOP = "browser_sign_in", "browser_wait_until", "browser_open_desktop"
COLOUR, TYPE_KEYS, HOLD = "browser_color_scheme", "browser_type_keys", "browser_hold_request"
RELEASE, PRESS, FAIL = "browser_release_request", "browser_mouse_press_xy", "browser_fail_request"
SCHEMES = ("light", "dark")
# Playwright's names for the network errors a request can fail with.
ERRORS = (
    "aborted", "accessdenied", "addressunreachable", "blockedbyclient", "blockedbyresponse", "connectionaborted",
    "connectionclosed", "connectionfailed", "connectionrefused", "connectionreset", "internetdisconnected",
    "namenotresolved", "timedout", "failed",
)  # fmt: skip

PATTERN = {"type": "string", "description": "the address, * and ** as wildcards: **/api/runs/*"}

# A line added to one of Playwright's own tools, where a tester reads it as it loads that tool.
NOTES = {
    "browser_type": f"On a remote desktop's live view, type with {TYPE_KEYS} instead: there this tool loses capitals, "
    "symbols and keys sent fast.",
    "browser_mouse_click_xy": f"On a remote desktop's live view, click with {PRESS} instead: a click this short may "
    "not take there.",
    WAIT_FOR: "A text or textGone wait gives up after a few seconds, and time sleeps before either, so a message that "
    f"shows briefly can pass unseen: wait on its text alone, right after the step. {WAIT_UNTIL} waits longer, and "
    "reloads the page only when asked.",
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
        "description": "Wait until a text shows on the page, up to a ceiling. One call does the whole wait. A reload "
        "is a step of its own, which can change what a step tests: reload only a page that shows news once loaded "
        "again. It reads the page's own text, never words drawn in a picture or a live remote view: check those "
        "with a screenshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "the words to wait for, as the page shows them"},
                "minutes": {"type": "number", "description": "the ceiling, in minutes"},
                "reload": {
                    "type": "boolean",
                    "description": "true reloads the page every so often; false leaves "
                    "it as it is, for a page that updates itself",
                },
            },
            "required": ["text", "minutes", "reload"],
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
                "pattern": PATTERN,
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
    {
        "name": FAIL,
        "description": "Fail the next request to an address once with a network error, as a dropped connection "
        "does; the one after goes through. A second call for an address replaces the first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": PATTERN,
                "error": {"type": "string", "enum": list(ERRORS)},
            },
            "required": ["pattern", "error"],
        },
    },
]
