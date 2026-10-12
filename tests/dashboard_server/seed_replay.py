"""One tester run's replay, as the warden's replays pass leaves it in the run's folder: three frames of a plain
sign-in form and the beats of two items, one failed on its second step and one with a step it did not run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from seed_kit import FIXTURES

FINDING = {"severity": "major", "text": "Continue does nothing on the first click"}


def _think(at: float, text: str) -> dict[str, Any]:
    return {"k": "think", "text": text, "at": at}


def _act(seg: list[float], kind: str, text: str, **more: Any) -> dict[str, Any]:
    return {"t": seg[0], "seg": seg, "k": kind, "text": text, **more}


def _step(
    n: int, name: str, expected: str, result: str, got: str, beats: list[dict[str, Any]], **more: Any
) -> dict[str, Any]:
    return {
        "n": n,
        "name": name,
        "expected": expected,
        "result": result,
        "got": got,
        "finding": None,
        "beats": beats,
        "still": None,
        **more,
    }


REPLAY: dict[str, Any] = {
    "version": 1,
    "frames": [
        {"t": 0.0, "file": "replay/frames/000001.jpg", "w": 1280, "h": 800},
        {"t": 4.1, "file": "replay/frames/000002.jpg", "w": 1280, "h": 800},
        {"t": 7.4, "file": "replay/frames/000003.jpg", "w": 1280, "h": 800},
    ],
    "hidden": [[8.6, 9.2]],
    "seconds": 9.4,
    "items": [
        {
            "item": "sign-in",
            "verdict": "fail",
            "at": 2,
            "severity": "major",
            "steps": [
                _step(
                    1,
                    "Open the sign-in page",
                    "The sign-in form shows",
                    "pass",
                    "The form showed",
                    [
                        _think(0.2, "I'll open the sign-in page first."),
                        _act([0.5, 1.5], "go", "Opened app.example.test/sign-in"),
                        _act(
                            [1.6, 2.2],
                            "read",
                            "Read “Sign in”",
                            outer=[400, 180, 480, 340],
                            inner=[[430, 210, 140, 40]],
                        ),
                    ],
                    still="replay/frames/000001.jpg",
                ),
                _step(
                    2,
                    "Sign in with a test address",
                    "The home page greets the tester",
                    "fail",
                    "The page stayed on the form",
                    [
                        _think(2.5, "Now I'll type the address and continue."),
                        _act([3.0, 4.3], "type", "friend@example.test", enter=False, x=640, y=330, down=3.2),
                        _act([5.0, 7.6], "click", "Clicked “Continue”", x=640, y=420, down=6.9),
                        _act([7.7, 8.1], "miss", "Didn't work: gave up after 5 s"),
                        _act([8.3, 8.3], "shot", "Took a screenshot"),
                    ],
                    finding=FINDING,
                    still="replay/frames/000002.jpg",
                ),
            ],
        },
        {
            "item": "profile",
            "verdict": "pass",
            "at": None,
            "severity": None,
            "steps": [
                _step(
                    1,
                    "Open the profile",
                    "The profile shows",
                    "pass",
                    "It showed",
                    [_act([8.4, 9.4], "wait", "Waited for “Profile”")],
                    still="replay/frames/000003.jpg",
                ),
                _step(2, "Change the name", "The new name shows", "skip", "", []),
            ],
        },
    ],
}


def seed_replay(run: Path) -> None:
    (run / "replay" / "frames").mkdir(parents=True)
    for n in (1, 2, 3):
        picture = FIXTURES / "pictures" / f"replay-{n}.jpg"
        (run / "replay" / "frames" / f"{n:06d}.jpg").write_bytes(picture.read_bytes())
    (run / "replay" / "replay.json").write_text(json.dumps(REPLAY))
