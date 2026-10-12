"""A run's Playwright trace, read for its replay: every frame the screen showed, on the wall clock Chrome stamped it
with, and the page recorder's lines (recorder.js), which the trace keeps as console messages.

Chrome stamps each frame with the moment it swapped it onto the screen (``frameSwapWallTime``), and the recorder
stamps each line with the page's own wall clock: both are this machine's clock, as the transcript's times are, so
nothing needs aligning.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RECORDER = "[qa-rec] "
_PICTURE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ReplayError(Exception):
    """A run's replay cannot be built; the message, which the owner reads beside the run, says why."""


@dataclass(frozen=True, slots=True)
class Frame:
    at: float  # wall seconds
    file: str  # the picture's name in the trace's resources
    width: int
    height: int


@dataclass(slots=True)
class Trace:
    before: float  # replay.recorder_before_seconds
    after: float  # replay.recorder_after_seconds
    frames: list[Frame] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)  # the recorder's lines, each with ``at`` in seconds

    def near(self, kind: str, start: float, end: float) -> list[dict[str, Any]]:
        """The recorder's lines of ``kind`` from just before ``start`` to a little after ``end``: a click lands a
        moment after its call is sent, and its line is written a moment after it lands."""
        return [e for e in self.events if e["k"] == kind and start - self.before <= e["at"] <= end + self.after]


def read(folder: Path, before: float, after: float) -> Trace:
    """The run's one trace, from ``folder``/traces; ReplayError when there is none or it holds no frame."""
    found = sorted((folder / "traces").glob("*.trace"))
    if len(found) != 1:
        raise ReplayError(f"the run has {len(found)} traces, not one, so it cannot be replayed")
    trace = Trace(before, after)
    with found[0].open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            try:
                event = json.loads(line)
            # telemetry: expected — the trace's last line may be cut where the browser stopped writing
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "screencast-frame":
                trace.frames.append(_frame(event))
            elif event.get("type") == "console" and str(event.get("text", "")).startswith(RECORDER):
                recorded = _recorded(str(event["text"]))
                if recorded is not None:
                    trace.events.append(recorded)
    if not trace.frames:
        raise ReplayError("the run's trace holds no frame of the screen")
    trace.frames.sort(key=lambda f: f.at)
    trace.events.sort(key=lambda e: e["at"])
    return trace


def _frame(event: dict[str, Any]) -> Frame:
    try:
        frame = Frame(
            float(event["frameSwapWallTime"]) / 1000, str(event["sha1"]), int(event["width"]), int(event["height"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayError(f"a frame in the trace lacks its time, picture or size ({exc})") from exc
    # The tester's browser wrote the trace: a picture is named inside its resources, never by a path out of them.
    if not _PICTURE.match(frame.file):
        raise ReplayError("a frame in the trace names its picture by a path, so the trace cannot be trusted")
    return frame


def _recorded(text: str) -> dict[str, Any] | None:
    try:
        line = json.loads(text.removeprefix(RECORDER))
    # telemetry: expected — a page's own console line that happens to start like the recorder's is not its
    except ValueError:
        return None
    if not isinstance(line, dict) or not isinstance(line.get("k"), str) or not isinstance(line.get("t"), int | float):
        return None
    return {**line, "at": float(line["t"]) / 1000}
