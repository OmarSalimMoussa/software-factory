"""The imports the warden runs on a timer: prod's and staging's (slot 0's) new errors in the tick, and the users' new
reports and the chat's tagged messages in passes of their own, off the tick (``Background``).

Each keeps, in its own state file, when it last ran, since when its runs have failed, and the note `factory status`
shows. A run of failures is logged as a warning when it begins and, once it has lasted
``watch.failing_alarm_minutes``, as an error: a failure that lasts is a platform failure, not a blip. A failing run is
tried again at the next period; it never stops the tick.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from runtime.warden.context import Context

log = logging.getLogger(__name__)

TIMER_KEYS = ("polled", "failing_since", "note")


@dataclass(frozen=True, slots=True)
class Timed:
    what: str  # the note's first words, such as "errors import"
    doing: str  # the log's words for the work, such as "importing prod's errors"
    setting: str  # the minutes between runs
    expected: tuple[type[Exception], ...]  # what a failing run is expected to raise; the rest is logged with its trace
    told: tuple[type[Exception], ...]  # failures whose text names the cause and never a secret; the rest by class


def fresh() -> dict[str, Any]:
    return {"polled": None, "failing_since": None, "note": None}


def run(ctx: Context, timed: Timed, state: dict[str, Any], work: Callable[[], str]) -> bool:
    """Run ``work`` once its period has passed; whether it ran. The caller writes ``state``, the timer keys."""
    now, last = ctx.clock.now(), state["polled"]
    if last is not None and now - last < ctx.settings().get_int(timed.setting) * 60:
        return False
    state["polled"] = now
    try:
        found = work()
    except Exception as exc:  # noqa: BLE001 - one import that fails must not stop the warden's loop
        why = str(exc) if isinstance(exc, timed.told) else type(exc).__name__
        since = state["failing_since"]
        alarm = ctx.settings().get_int("watch.failing_alarm_minutes") * 60
        if since is None:
            log.warning("warden: %s failed (%s); it runs again at the next poll", timed.doing, why,
                        exc_info=not isinstance(exc, timed.expected))  # fmt: skip
            state["failing_since"] = now
        elif last - since < alarm <= now - since:
            log.error("warden: %s has failed for %d minutes (%s); it still runs at each poll", timed.doing,
                      (now - since) // 60, why)  # fmt: skip
        state["note"] = f"{timed.what}: the read at {ctx.clock.iso()} failed ({why}); it runs again at the next poll"
    else:
        state["failing_since"] = None
        state["note"] = f"{timed.what}: read at {ctx.clock.iso()}; {found}"
    return True


def note(state: dict[str, Any]) -> str | None:
    found = state["note"]
    return found if isinstance(found, str) else None


Start = Callable[[Callable[[], None]], threading.Thread | None]


def in_thread(name: str) -> Start:
    """Run work in a daemon thread of its own, named ``name``: the start a pass uses for slow work the tick must not
    wait on (a test passes one that runs it at once)."""

    def start(work: Callable[[], None]) -> threading.Thread:
        thread = threading.Thread(target=work, name=name, daemon=True)
        thread.start()
        return thread

    return start


class Background:
    """A pass whose work runs in a thread of its own, one at a time: while the last one runs, a pass starts none."""

    def __init__(self, work: Callable[[Context], None], start: Start) -> None:
        self._work, self._start = work, start
        self._thread: threading.Thread | None = None

    def run_pass(self, ctx: Context) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = self._start(lambda: self._work(ctx))
