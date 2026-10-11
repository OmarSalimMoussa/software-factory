"""The guard's own time. Claude Code lets a call through when its hook runs past install.hook_timeout_seconds, so the
guard gives itself half of that: when it runs out, whatever the guard was doing stops and the call is refused, with
the other half left to say so. The hook arms it once per call; every wait inside the guard takes what is left.
Stdlib only, like the wire: it must work when a rule module does not.
"""

from __future__ import annotations

import signal
import time
from types import FrameType


class OutOfTime(Exception):
    """The guard used its half of the hook's time; the call is refused."""


_until: float | None = None
_spare = 0.0


def _ring(signum: int, frame: FrameType | None) -> None:
    raise OutOfTime("the guard ran out of time to decide")


def arm(hook_seconds: float) -> None:
    """Give this hook run half of ``hook_seconds``; past it, the running code raises OutOfTime."""
    global _until, _spare
    _spare = hook_seconds / 2
    _until = time.monotonic() + _spare
    signal.signal(signal.SIGALRM, _ring)
    signal.setitimer(signal.ITIMER_REAL, _spare)


def disarm() -> None:
    global _until
    signal.setitimer(signal.ITIMER_REAL, 0)
    _until = None


def left() -> float | None:
    """Seconds left to decide; None when unarmed (a run outside the hook, such as a test). Raises OutOfTime at 0."""
    if _until is None:
        return None
    remaining = _until - time.monotonic()
    if remaining <= 0:
        raise OutOfTime("the guard ran out of time to decide")
    return remaining


def spare() -> float | None:
    """The half kept back for saying what happened: how long a refusal may take to send; None when unarmed."""
    return _spare if _until is not None else None
