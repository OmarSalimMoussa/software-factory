"""Playwright's tools as the factory's own tools call them, on a clock of the test's own."""

from __future__ import annotations

import json
import threading
from typing import Any

from runtime.qa.wrapper import ServerError
from runtime.qa.wrapper_tools import Tools

LINK = "https://app.test/auth/link/one-time-secret"


class Ticks(threading.Event):
    """A cancel flag whose waits pass on a clock of their own, and never end early."""

    def __init__(self) -> None:
        super().__init__()
        self.clock = 0.0

    def wait(self, timeout: float | None = None) -> bool:
        self.clock += timeout or 0
        return self.is_set()


class Browser:
    """Playwright's tools as the factory's tools call them: each call kept, each snapshot the next page given."""

    def __init__(self, pages: list[str], results: tuple[Any, ...] = (), misses: int = 0) -> None:
        self.pages, self.results, self.ticks, self.misses = iter(pages), iter(results), Ticks(), misses
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.at: list[float] = []  # each call's time on the wait's clock

    def call(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        self.at.append(self.ticks.clock)
        if name == "browser_snapshot":
            return next(self.pages)
        if name == "browser_wait_for" and "text" in args:
            if self.misses:
                self.misses -= 1
                self.ticks.clock += 5  # Playwright's own wait for a text ends at its action timeout
                raise ServerError("browser_wait_for failed: TimeoutError: locator.waitFor: Timeout 5000ms exceeded")
            return f"Waited for {args['text']}"
        # The code tool's value as Playwright prints it.
        return next((f"### Result\n{json.dumps(r)}\n### Ran Playwright code" for r in self.results), f"{name} done")

    def answer(
        self, name: str, args: dict[str, Any], reload_seconds: int = 60, max_minutes: int = 15
    ) -> tuple[str, bool]:
        tools = Tools(
            self.call,
            lambda address: LINK,
            lambda link: None,
            lambda address: None,
            reload_seconds,
            max_minutes,
            (1280, 800),
            40,
            30,
            150,
            250,
            lambda: self.ticks.clock,
        )
        return tools.answer(name, args, self.ticks)
