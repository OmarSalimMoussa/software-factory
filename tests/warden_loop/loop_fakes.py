"""Fakes for the warden loop's tests: the clock, GitHub and the health addresses."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.warden.context import Clock, Context, StackResult
from runtime.warden.probes import Answer


class FakeClock(Clock):
    def __init__(self) -> None:
        self.t = 1_791_000_000.0

    def now(self) -> float:
        return self.t


@dataclass
class FakeGitHub:
    up: bool = True
    on_run: Callable[[], None] | None = None
    dispatched: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)  # what each dispatch said the last releases ran at
    releases: str = "success"  # how the prod workflow's jobs after its deploy job end
    ci: dict[str, Any] | None = None  # CI's run of a pushed commit, once GitHub started one

    def dispatch_promote(self, sha: str, released: str) -> str:
        self.dispatched.append(sha)
        self.released.append(released)
        return "42"

    def full_sha(self, sha: str) -> str:
        return sha + "f" * (40 - len(sha))

    def ci_run(self, sha: str) -> str | None:
        return None if self.ci is None else "ci-7"

    def run(self, run_id: str) -> dict[str, Any]:
        if run_id == "ci-7" and self.ci is not None:
            return self.ci
        if self.on_run is not None:
            self.on_run()
        return {
            "id": run_id,
            "status": "completed",
            "conclusion": self.releases,
            "url": "https://github.test/42",
            "jobs": {"deploy": {"status": "completed", "conclusion": "success"}},
        }

    def reachable(self) -> bool:
        return self.up


@dataclass
class FakeStack:
    """scripts/dev on slot 0, for the tick's report pull: the product lists the verbs ``verbs`` names."""

    logs: Path
    verbs: str = "up down health"
    calls: list[tuple[int, list[str], str]] = field(default_factory=list)

    def run_stack(self, slot: int, verb: list[str], session: str) -> StackResult:
        self.calls.append((slot, verb, session))
        log = self.logs / f"stack-{len(self.calls)}.log"
        log.write_text(self.verbs if verb == ["verbs"] else "")
        return StackResult(0, "", str(log))


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
