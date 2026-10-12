"""A step's beats put in the order they happened on screen, so the replay never goes back in time.

A tester often sends calls together (a wait and a screenshot, a reload and a wait), and they finish in another order
than they were sent: played in sent order, the screen would jump back to a past frame. So each beat is placed by
when it showed, and none starts before the one before it ended.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Timings:
    long_wait: float  # replay.long_wait_seconds
    wait_tail: float  # replay.wait_tail_seconds
    navigation_tail: float  # replay.navigation_tail_seconds


def on_screen(beat: dict[str, Any], timings: Timings) -> list[float]:
    """The part of the recording a beat plays: [start, end]."""
    start, end = beat["seg"]
    if beat["k"] == "wait" and end - start > timings.long_wait:
        # The page does not change while the tester waits: only the end is shown.
        return [end - timings.wait_tail, end]
    if beat["k"] == "go":
        # A page change can queue behind a wait sent with it; its page arrives at its end.
        return [max(start, end - timings.navigation_tail), end]
    if beat["k"] == "shot" or (beat["k"] == "miss" and "x" not in beat):
        # Taken, or refused, just before its answer came back.
        return [end, end]
    return [start, end]


def order(beats: list[dict[str, Any]], timings: Timings) -> list[dict[str, Any]]:
    """The beats in the order they showed, each starting no earlier than the one before it ended. A thought keeps
    the time it was written, and moves no recording."""
    placed = []
    for index, beat in enumerate(beats):
        if beat["k"] == "think":
            placed.append((beat["at"], index, dict(beat)))
        else:
            moved = dict(beat, seg=on_screen(beat, timings))
            placed.append((moved["seg"][0], index, moved))
    out = [beat for _, _, beat in sorted(placed, key=lambda p: (p[0], p[1]))]
    head: float | None = None
    for beat in out:
        if beat["k"] == "think":
            continue
        start, end = beat["seg"]
        if head is not None and start < head:
            start, end = head, max(end, head)
        beat["seg"], beat["t"] = [round(start, 2), round(end, 2)], round(start, 2)
        if "down" in beat:
            beat["down"] = round(min(max(beat["down"], start), end), 2)
        head = end
    return out
