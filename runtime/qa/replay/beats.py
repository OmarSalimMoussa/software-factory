"""The tester's own words and actions, from its transcript, as the replay's beats: each thought it wrote and each
browser call it made, with what the page recorder saw of it (where a click landed, which field took the typing,
what a read touched), on the replay's clock, whose second 0 is the run's first frame.

The tester's calls are known by name and input only; their words here are the replay's, never the tester's.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from runtime.guard.browser import BROWSER
from runtime.qa.replay.trace import Trace
from runtime.qa.wrapper import ROUTE, WAIT_FOR
from runtime.qa.wrapper_tools import HOLD, LINK, OPEN_DESKTOP, RELEASE, SIGN_IN, WAIT_UNTIL

# Calls with nothing to show: the browser's own housekeeping.
QUIET = frozenset({"browser_close", "browser_tabs", "browser_install"})
SHOT = re.compile(r"\]\(\./([^)/]+\.(?:png|jpe?g))\)")
# The desktop's one-time password, as `desktop up` prints it after the word password; JSON-escaped, as LINK says.
_PASSWORD = re.compile(r"password\W{1,3}([^\s'\"\\,]{4,})", re.IGNORECASE)
_TIMEOUT = re.compile(r"Timeout (\d+)ms")
_MATCHED = re.compile(r"resolved to (\d+)")


@dataclass(slots=True)
class Use:
    at: float
    name: str
    input: dict[str, Any]
    done: float | None = None
    result: str = ""
    failed: bool = False


@dataclass(slots=True)
class Transcript:
    thoughts: list[tuple[float, str]] = field(default_factory=list)  # (wall seconds, text)
    uses: list[Use] = field(default_factory=list)
    secrets: set[str] = field(default_factory=set)  # sign-in links and desktop passwords the tester was handed


def wall(stamp: object) -> float | None:
    """An ISO time in wall seconds; None when it is not one."""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    # telemetry: expected — an entry with no readable time cannot be placed on the clock, so it is left out
    except ValueError:
        return None


def read(path: Path) -> Transcript:
    """The thoughts and calls in a tester's transcript, and the secrets it was handed."""
    found = Transcript()
    open_uses: dict[str, Use] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(raw)
        # telemetry: expected — the line being written when the run ended is cut short
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        at = wall(entry.get("timestamp")) if isinstance(entry, dict) else None
        if not isinstance(message, dict) or at is None or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and entry.get("type") == "assistant" and str(block.get("text", "")).strip():
                found.thoughts.append((at, re.sub(r"\s+", " ", str(block["text"])).strip()))
            elif kind == "tool_use":
                given = block.get("input")
                use = Use(at, str(block.get("name", "")), given if isinstance(given, dict) else {})
                open_uses[str(block.get("id"))] = use
                found.uses.append(use)
            elif kind == "tool_result" and str(block.get("tool_use_id")) in open_uses:
                use = open_uses[str(block["tool_use_id"])]
                use.done, use.result = at, _text(block.get("content"))
                use.failed = bool(block.get("is_error")) or use.result.lstrip().startswith("### Error")
                found.secrets.update(_handed(use))
    return found


def _handed(use: Use) -> list[str]:
    """The secrets a call printed for the tester: a sign-in link, or the desktop's password."""
    command = str(use.input.get("command", "")) if use.name == "Bash" else ""
    if "slot sign-in" in command:
        return LINK.findall(use.result)
    if "slot desktop up" in command:
        return _PASSWORD.findall(use.result)
    return []


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""


def short(url: str) -> str:
    """A page's address as a person reads it: its host and path, no scheme, query or fragment."""
    parts = urlparse(url)
    return (parts.netloc + (parts.path if parts.path not in ("", "/") else "")).removeprefix("www.")


def _why(result: str) -> str:
    first = next((line for line in result.splitlines() if line.strip() and not line.startswith("###")), "")
    if timeout := _TIMEOUT.search(first):
        return f"gave up after {round(int(timeout.group(1)) / 1000)} s"
    if "parsing css selector" in first:
        return "named its target in a way the tool can't read"
    if "strict mode violation" in first:
        matched = _MATCHED.search(first)
        return f"{matched.group(1) if matched else 'several'} elements matched"
    return first[:60]


@dataclass(frozen=True, slots=True)
class Shares:
    """How much of the screen an element takes to be read as a whole view, as the settings say."""

    desktop: float  # replay.desktop_view_share: a field this large is a desktop's live view, which takes all keys
    part: float  # replay.read_part_share: a looked-up element smaller than this is a part of the page


@dataclass(frozen=True, slots=True)
class Seen:
    """One finished call, with what the recorder saw while it ran."""

    use: Use
    trace: Trace
    t0: float
    last: list[tuple[int, int]]  # the tester's last click on a desktop, where its typing then lands
    downs: list[dict[str, Any]]
    shares: Shares


def beat(use: Use, trace: Trace, t0: float, last: list[tuple[int, int]], shares: Shares) -> dict[str, Any] | None:
    """One browser call as a beat, or None for a call with nothing to show."""
    name = use.name.removeprefix(BROWSER)
    if not use.name.startswith(BROWSER) or name in QUIET or use.done is None:
        return None
    start, end = round(use.at - t0, 2), round(use.done - t0, 2)
    seen = Seen(use, trace, t0, last, trace.near("down", use.at, use.done), shares)
    if use.failed:
        missed: dict[str, Any] = {"k": "miss", "text": "Didn't work: " + _why(use.result)}
        if seen.downs:
            missed |= {"x": seen.downs[0]["x"], "y": seen.downs[0]["y"]}
        return {"t": start, "seg": [start, end], **missed}
    mouse = name.startswith("browser_mouse_") and "x" in use.input
    return {"t": start, "seg": [start, end], **(_mouse(seen) if mouse else _SHOWN.get(name, _other)(seen))}


def _go(seen: Seen) -> dict[str, Any]:
    name, args = seen.use.name.removeprefix(BROWSER), seen.use.input
    if name == SIGN_IN:
        return {"k": "go", "text": f"Signed in as {args.get('address', 'a test address')}"}
    what = "the remote desktop" if name == OPEN_DESKTOP else short(str(args.get("url", "")))
    return {"k": "go", "text": f"Opened {what}"}


def _mouse(seen: Seen) -> dict[str, Any]:
    name, args = seen.use.name.removeprefix(BROWSER), seen.use.input
    verb = {"browser_mouse_move_xy": "Moved", "browser_mouse_drag_xy": "Dragged"}.get(name, "Clicked")
    point = (round(float(args["x"])), round(float(args["y"])))
    seen.last[:] = [point]
    found: dict[str, Any] = {"k": "click", "x": point[0], "y": point[1], "text": verb}
    return found | ({"down": round(seen.downs[0]["at"] - seen.t0, 2)} if seen.downs else {})


def _click(seen: Seen) -> dict[str, Any]:
    if not seen.downs:
        return {"k": "click", "text": "Clicked"}
    down = seen.downs[0]
    what = str(down.get("name") or seen.use.input.get("element", ""))[:40]
    return {
        "k": "click",
        "x": down["x"],
        "y": down["y"],
        "down": round(down["at"] - seen.t0, 2),
        "text": f"Clicked “{what}”" if what else "Clicked",
    }


def _typed(seen: Seen) -> dict[str, Any]:
    use, trace, t0, last = seen.use, seen.trace, seen.t0, seen.last
    text, enter = str(use.input.get("text", "")), bool(use.input.get("submit"))
    typed: dict[str, Any] = {"k": "type", "text": text, "enter": enter}
    assert use.done is not None  # beat() places only finished calls
    # The first sign of the typing, so the replay pauses on the screen before any of it shows.
    signs = sorted(
        trace.near("focus", use.at, use.done)
        + trace.near("input", use.at, use.done)
        + trace.near("key", use.at, use.done),
        key=lambda e: e["at"],
    )
    boxed = next((e for e in signs if e.get("box")), None)
    if boxed is None:
        return typed
    box, down = boxed["box"], round(signs[0]["at"] - t0, 2)
    if box[2] * box[3] > seen.shares.desktop * boxed["vw"] * boxed["vh"] and last:
        # Typing into a desktop's live view: the whole view takes the keys, so the field is the last click's point.
        return typed | {"x": last[0][0], "y": last[0][1], "down": down}
    return typed | {"x": round(box[0] + box[2] / 2), "y": round(box[1] + box[3] / 2), "down": down}


def _read(seen: Seen) -> dict[str, Any]:
    """A read of the page: what the tester's script looked up, the element it used drawn solid, the part of the page
    it searched dashed."""
    use, trace, name = seen.use, seen.trace, seen.use.name.removeprefix(BROWSER)
    assert use.done is not None  # beat() places only finished calls
    reads = trace.near("read", use.at, use.done)
    found: dict[str, Any] = {"k": "read", "text": "Read the page"}
    if name == "browser_evaluate" and "innerWidth" in str(use.input.get("function", "")):
        found["text"] = "Read the window's width"
    boxes = [
        (b, str(n or ""))
        for r in reads
        for b, n in zip(r.get("boxes", []), r.get("names", []), strict=False)
        if b and b[2] > 4 and b[3] > 4 and b[0] < r["vw"] and b[1] < r["vh"] and b[0] + b[2] > 0 and b[1] + b[3] > 0
    ]
    if not boxes:
        return found
    screen = reads[0]["vw"] * reads[0]["vh"]
    answer = _squash(use.result.split("### Ran Playwright code")[0])
    used: list[tuple[list[int], str]] = []
    for box, label in boxes:
        if len(_squash(label)) >= 4 and _squash(label)[:16] in answer and box not in [u[0] for u in used]:
            used.append((box, label))
    used = [u for u in used if not any(_inside(v[0], u[0]) for v in used)][:4]
    parts = [b for b in boxes if b[0][2] * b[0][3] < seen.shares.part * screen]
    if parts:
        outer = max(parts, key=lambda b: b[0][2] * b[0][3])[0]
        if outer not in [u[0] for u in used]:
            found["outer"] = outer
    if used:
        found["inner"] = [u[0] for u in used]
        what = used[0][1].strip()
        more = f" and {len(used) - 1} more" if len(used) > 1 else ""
        found["text"] = f"Read “{what[:24]}{'…' if len(what) > 24 else ''}”{more}"
    return found


# The calls whose words need nothing but their input: (kind, words).
_WORDS: dict[str, Callable[[dict[str, Any]], tuple[str, str]]] = {
    WAIT_FOR: lambda a: ("wait", f"Waited {a['time']} s" if "time" in a else f"Waited for “{_bit(a)}”"),
    WAIT_UNTIL: lambda a: ("wait", f"Waited for “{_bit(a)}”"),
    "browser_resize": lambda a: ("resize", f"Set the window to {a.get('width')} × {a.get('height')}"),
    "browser_console_messages": lambda a: ("read", "Read the console"),
    "browser_network_requests": lambda a: ("read", "Read the requests"),
    "browser_press_key": lambda a: ("key", f"Pressed {a.get('key', '')}"),
    ROUTE: lambda a: ("read", f"Answered {str(a.get('pattern', ''))[:40]} with {a.get('status', 200)}"),
    HOLD: lambda a: ("wait", f"Held the next request to {str(a.get('pattern', ''))[:40]}"),
    RELEASE: lambda a: ("wait", f"Let the held request to {str(a.get('pattern', ''))[:40]} go on"),
}


def _bit(args: dict[str, Any]) -> str:
    return str(args.get("text", ""))[:30]


def _other(seen: Seen) -> dict[str, Any]:
    name = seen.use.name.removeprefix(BROWSER)
    if name == "browser_take_screenshot":
        shot = SHOT.search(seen.use.result)
        return {"k": "shot", "text": "Took a screenshot", **({"file": shot.group(1)} if shot else {})}
    if name in _WORDS:
        kind, text = _WORDS[name](seen.use.input)
        return {"k": kind, "text": text}
    return {"k": "read", "text": name.removeprefix("browser_").replace("_", " ").capitalize()}


_SHOWN: dict[str, Callable[[Seen], dict[str, Any]]] = {
    "browser_navigate": _go,
    OPEN_DESKTOP: _go,
    SIGN_IN: _go,
    "browser_click": _click,
    "browser_type": _typed,
    "browser_evaluate": _read,
    "browser_snapshot": _read,
}


def _squash(text: str) -> str:
    return re.sub(r"\W+", "", text).lower()


def _inside(a: list[int], b: list[int]) -> bool:
    return a != b and b[0] <= a[0] and b[1] <= a[1] and a[0] + a[2] <= b[0] + b[2] and a[1] + a[3] <= b[1] + b[3]
