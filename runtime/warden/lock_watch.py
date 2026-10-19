"""The lock watch: where the warden's threads were when its one lock was held past the dashboard's wait.

Every snapshot the dashboard asks for waits on that lock, so a hold longer than ``dashboard.warden_seconds`` shows the
warden as down. The watch tries the lock with that wait, then rests as long, so it catches every hold of twice the
wait. It logs a hold it catches once, naming the factory's own innermost frames of every other thread, and waits for
the hold to end before it tries again.
"""

from __future__ import annotations

import logging
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

_FACTORY = f"{Path(__file__).parents[2]}/"
_FRAMES = 2  # a thread's innermost line, and the line that called it: enough to place it, short enough to log


def watch(lock: threading.RLock, wait: Callable[[], float], stop: threading.Event) -> None:
    """Watch until ``stop`` is set, which the warden never does: the watch ends with its process."""
    while not stop.is_set():
        seconds = wait()
        if lock.acquire(timeout=seconds):
            lock.release()
            stop.wait(seconds)
            continue
        log.warning(
            "warden: its lock was held past the dashboard's %g s wait, so the dashboard showed the warden as down; %s",
            seconds,
            _where(),
        )
        with lock:
            pass


def _where() -> str:
    """Every other thread in the factory's code, by name, at its innermost frames; threads at one place share it."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    found: dict[str, list[str]] = {}
    for ident, frame in sys._current_frames().items():
        own = [
            f"{f.f_code.co_filename.removeprefix(_FACTORY)}:{line} in {f.f_code.co_name}"
            for f, line in traceback.walk_stack(frame)
            if f.f_code.co_filename.startswith(_FACTORY)
        ]
        if own and ident != threading.get_ident():
            found.setdefault(" < ".join(own[:_FRAMES]), []).append(names.get(ident, str(ident)))
    return (
        "; ".join(f"{', '.join(sorted(who))} at {at}" for at, who in sorted(found.items()))
        or "no other thread is in the factory's code"
    )
