"""The tester's calls its browsers have yet to answer (wrapper.py): the browser each went to, each on a clock that
runs ``late`` once it is past due, and those the session was told timed out, whose late answers go nowhere.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any


class Calls:
    def __init__(self, late: Callable[[Any, str, float], None]) -> None:
        self._late, self._lock = late, threading.Lock()
        self._out: dict[str, tuple[threading.Timer, str, Any]] = {}  # by the id's text: the clock, browser and id
        self._overdue: set[str] = set()

    def sent(self, key: Any, which: str, name: str, limit: float) -> None:
        timer = threading.Timer(limit, self._late, args=(key, name, limit))
        timer.daemon = True
        with self._lock:
            self._out[str(key)] = (timer, which, key)
        timer.start()

    def where(self, key: Any) -> str | None:
        with self._lock:
            found = self._out.get(str(key))
        return found[1] if found else None

    def arrived(self, key: Any) -> bool:
        """Its answer came, or the session cancelled it: False when the session was told it timed out."""
        with self._lock:
            found = self._out.pop(str(key), None)
            late = str(key) in self._overdue
            self._overdue.discard(str(key))
        if found is not None:
            found[0].cancel()
        return not late

    def give_up(self, key: Any) -> str | None:
        """Stop waiting for a call still out: the browser it went to, or None when it is not out."""
        with self._lock:
            found = self._out.pop(str(key), None)
            if found is None:
                return None
            self._overdue.add(str(key))
        found[0].cancel()
        return found[1]

    def out(self, which: str) -> list[Any]:
        with self._lock:
            return [key for _, at, key in self._out.values() if at == which]

    def stop(self) -> None:
        with self._lock:
            for timer, _, _ in self._out.values():
                timer.cancel()
            self._out.clear()
