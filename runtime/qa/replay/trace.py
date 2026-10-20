"""A run's Playwright traces, read for its replay: every frame the screen showed, on the wall clock Chrome stamped it
with, and the page recorder's lines (recorder.js), which the trace keeps as console messages. Each frame names the
width of the page it showed, which a tester's hand-back is checked against.

Chrome stamps each frame with the moment it swapped it onto the screen (``frameSwapWallTime``), and the recorder
stamps each line with the page's own wall clock: both are this machine's clock, as the transcript's times are, so
nothing needs aligning. Each browser writes a trace of its own into the run's ``traces/``: the first browser's starts
before the tester's first call, and the second's each time it opens, later. Playwright names a trace by the moment it
started, so the first by name is the first browser's, and the rest are the second's.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.qa.wrapper import FIRST, SECOND

RECORDER = "[qa-rec] "
FRAME = "screencast-frame"
# One plain name in the trace's resources: Playwright names a frame's picture ``page@<the page's id>-<ms>.jpeg``.
# No slash, backslash or leading dot, so no name reaches out of the folder.
_PICTURE = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,127}")


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


def read(folder: Path, before: float, after: float) -> dict[str, Trace]:
    """Each browser's trace from ``folder``/traces; ReplayError when there is none or the first's holds no frame."""
    found = sorted((folder / "traces").glob("*.trace"))
    if not found:
        raise ReplayError("the run has no trace, so it cannot be replayed")
    traces = {FIRST: Trace(before, after)}
    for n, path in enumerate(found):
        _read_into(traces.setdefault(SECOND if n else FIRST, Trace(before, after)), path)
    if not traces[FIRST].frames:
        raise ReplayError("the run's trace holds no frame of the screen")
    for trace in traces.values():
        trace.frames.sort(key=lambda f: f.at)
        trace.events.sort(key=lambda e: e["at"])
    return traces


def _read_into(trace: Trace, path: Path) -> None:
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line in lines:
            try:
                event = json.loads(line)
            # telemetry: expected — the trace's last line may be cut where the browser stopped writing
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == FRAME:
                trace.frames.append(_frame(event))
            elif event.get("type") == "console" and str(event.get("text", "")).startswith(RECORDER):
                recorded = _recorded(str(event["text"]))
                if recorded is not None:
                    trace.events.append(recorded)


def widths(lines: Iterable[str]) -> set[int]:
    """The widths of the page a trace's frames showed: Playwright names the viewport each frame was drawn at."""
    found: set[int] = set()
    for line in lines:
        # Most lines are copies of the page, too large to parse for nothing.
        if f'"{FRAME}"' not in line:
            continue
        try:
            event = json.loads(line)
        # telemetry: expected — the trace's last line may be cut where the browser is writing it
        except ValueError:
            continue
        width = event.get("width") if isinstance(event, dict) and event.get("type") == FRAME else None
        if isinstance(width, int):
            found.add(width)
    return found


def _frame(event: dict[str, Any]) -> Frame:
    try:
        frame = Frame(
            float(event["frameSwapWallTime"]) / 1000, str(event["sha1"]), int(event["width"]), int(event["height"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayError(f"a frame in the trace lacks its time, picture or size ({exc})") from exc
    # The tester's browser wrote the trace: a picture is named inside its resources, never by a path out of them.
    if not _PICTURE.fullmatch(frame.file):
        raise ReplayError(
            "a frame in the trace names its picture by more than a plain name, so the trace cannot be trusted"
        )
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
