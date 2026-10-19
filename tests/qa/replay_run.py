"""A small tester run as it reaches the replays pass: the trace's frames and the page recorder's lines, the tester's
transcript, its step marks and its finding. Second 0 is the first frame; the tester walks two steps of the `cart`
scenario, opens the desktop, types a sign-in link and opens another on the way, and never reaches step 3."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.qa.replay.beats import Shares
from runtime.qa.replay.build import Inputs
from runtime.qa.replay.timeline import Timings
from runtime.qa.scenarios import Scenario, Step

T0 = 1_791_554_520.0
LINK = "https://app.test/auth/link/one-time-secret"
HANDED = "https://app.test/auth/link/handed-in-the-transcript"
PASSWORD = "Xk92mQp7"
SECRET_SECONDS = 15
STEPS = (Step("Open", "The cart opens"), Step("Pay", "The order is placed"), Step("Receipt", ""))
SCENARIO = Scenario("cart", ("web/",), ("nothing",), "in-turn", 1, STEPS)


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(T0 + seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _recorded(at: float, **line: Any) -> dict[str, Any]:
    text = "[qa-rec] " + json.dumps({"t": (T0 + at) * 1000, "vw": 1280, "vh": 800, **line})
    return {"type": "console", "text": text}


def _said(at: float, text: str) -> dict[str, Any]:
    return {"type": "assistant", "timestamp": iso(at), "message": {"content": [{"type": "text", "text": text}]}}


SIGN_IN = f"A one-time link for tester-1@example.com:\n{HANDED}\nIt lasts 15 min."
DESKTOP = f"View: http://127.0.0.1:6080/vnc.html\nPassword: {PASSWORD}\n"


def _printed(tail: str) -> str:
    """What `factory slot` prints, as the transcript keeps it: the warden's answer as JSON."""
    return json.dumps({"ok": True, "slot": 1, "exit": 0, "tail": tail})


def _call(at: float, done: float, key: str, name: str, given: dict[str, Any], answer: str) -> list[dict[str, Any]]:
    use = {"type": "tool_use", "id": key, "name": name, "input": given}
    result = {"type": "tool_result", "tool_use_id": key, "content": [{"type": "text", "text": answer}]}
    return [
        {"type": "assistant", "timestamp": iso(at), "message": {"content": [use]}},
        {"type": "user", "timestamp": iso(done), "message": {"content": [result]}},
    ]


def picture(n: int) -> str:
    """Frame ``n``'s picture, named in the trace's resources as Playwright names it."""
    return f"page@61b379a7fb0f2ca28dfab3f58d509a1c-{round((T0 + n) * 1000)}.jpeg"


def make(run: Path, transcript: Path) -> Inputs:
    """The run's folder and the tester's transcript, written where they are named; what the builder is given."""
    (run / "traces" / "resources").mkdir(parents=True)
    events = []
    for n in range(12):
        name = picture(n)
        (run / "traces" / "resources" / name).write_bytes(f"jpeg {n}".encode())
        events.append(
            {
                "type": "screencast-frame",
                "sha1": name,
                "width": 1280,
                "height": 800,
                "frameSwapWallTime": (T0 + n) * 1000,
            }
        )
    events += [
        _recorded(4.2, k="down", x=640, y=420, name="Pay now"),
        _recorded(6.1, k="focus", box=[400, 300, 480, 40], name="email"),
        # A page the tester's script read: the cart, and its total inside it.
        _recorded(9.3, k="read", boxes=[[100, 100, 600, 400], [120, 140, 200, 30]], names=["Cart", "Total 12.00"]),
    ]
    (run / "traces" / "run.trace").write_text("\n".join(json.dumps(e) for e in events) + '\n{"cut', encoding="utf-8")
    (run / "secrets.json").write_text(json.dumps([LINK]))
    (run / "page-3.yml").write_text(f"- Page URL: {LINK}\n")
    (run / "page-9.yml").write_text("- Page URL: https://app.test/cart\n")
    (run / "shot-9.png").write_bytes(b"png")
    lines = [
        _said(0.3, "I'll open the cart first."),
        *_call(0.6, 1.8, "a", "mcp__qa-browser__browser_navigate", {"url": "https://app.test/cart?x=1"}, "ok"),
        _said(2.6, "Now I'll pay."),
        *_call(0.35, 0.45, "h", "Bash", {"command": "factory slot desktop up tester-1@example.com"}, _printed(DESKTOP)),
        *_call(3.0, 3.4, "b", "Bash", {"command": "factory slot sign-in tester-1@example.com"}, _printed(SIGN_IN)),
        *_call(4.0, 4.6, "c", "mcp__qa-browser__browser_click", {"element": "Pay now", "ref": "e5"}, "ok"),
        *_call(6.0, 6.5, "d", "mcp__qa-browser__browser_type", {"text": LINK, "submit": True}, "ok"),
        *_call(6.6, 6.7, "i", "mcp__qa-browser__browser_navigate", {"url": LINK}, "ok"),
        _said(7.0, f"The page still shows {HANDED}."),
        # Sent together: a long wait, and a screenshot that is answered first.
        *_call(7.2, 8.6, "f", "mcp__qa-browser__browser_take_screenshot", {}, "![shot](./shot-9.png)"),
        *_call(6.8, 11.0, "e", "mcp__qa-browser__browser_wait_for", {"time": 4}, "ok"),
        *_call(9.0, 9.6, "g", "mcp__qa-browser__browser_evaluate", {"function": "() => total()"}, "Total 12.00"),
    ]
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    records = [
        {"item": "cart", "step": 1, "agent": "a1", "at": iso(0.5)},
        {
            "item": "cart",
            "step": 1,
            "result": "pass",
            "got": f"The cart opened; the desktop took {PASSWORD}",
            "agent": "a1",
            "at": iso(2.2),
        },
        {"item": "cart", "step": 2, "agent": "a1", "at": iso(2.8)},
        {"item": "cart", "step": 2, "result": "fail", "got": f"It stayed on {HANDED}", "agent": "a1", "at": iso(11.2)},
    ]
    finding = {"step": "cart:2", "severity": "major", "text": f"Pay now does nothing at {LINK}\nSteps: …"}
    return Inputs(
        run,
        transcript,
        records,
        [finding],
        lambda name: SCENARIO if name == "cart" else None,
        Timings(4.0, 2.5, 1.5),
        (0.05, 0.3),
        Shares(0.3, 0.6),
        SECRET_SECONDS,
    )
