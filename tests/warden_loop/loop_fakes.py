"""Fakes for the warden loop's tests: the clock, GitHub and the health addresses."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.warden.context import Clock, Context
from runtime.warden.probes import Answer


class FakeClock(Clock):
    def __init__(self) -> None:
        self.t = 1_791_000_000.0

    def now(self) -> float:
        return self.t


@dataclass
class FakeGitHub:
    up: bool = True
    previous: dict[str, Any] | None = None
    on_run: Callable[[], None] | None = None
    dispatched: list[str] = field(default_factory=list)

    def compare(self, base: str, head: str) -> list[dict[str, Any]]:
        raise AssertionError("not used")

    def dispatch_promote(self, sha: str) -> str:
        self.dispatched.append(sha)
        return "42"

    def run(self, run_id: str) -> dict[str, Any]:
        if self.on_run is not None:
            self.on_run()
        return {"id": run_id, "status": "completed", "conclusion": "success", "url": "https://github.test/42"}

    def deployment(self, environment: str) -> dict[str, Any]:
        return {"current": None, "previous": self.previous}

    def reachable(self) -> bool:
        return self.up


@dataclass
class World:
    ctx: Context
    github: FakeGitHub
    clock: FakeClock
    events: list[tuple[str, dict[str, Any]]]
    checkout: Path


def answers(*replies: Answer | Exception) -> Callable[[str, float], Answer]:
    """A fetch that gives each reply in turn: an Answer, or an exception it raises."""
    queue = list(replies)

    def fetch(url: str, timeout: float) -> Answer:
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return fetch


def healthy(commit: str) -> Answer:
    return Answer(200, f'{{"build_commit": "{commit}"}}'.encode())
