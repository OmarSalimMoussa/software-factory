from __future__ import annotations

import logging
import re
import threading
import time

import pytest

from runtime.warden import lock_watch

WAIT = 0.05  # the dashboard's wait for the warden, in seconds: short, so a hold past it takes a moment
ROUNDS = 3
HOLDER = re.compile(r"(^|; )tick at [^;]*tests/warden_services/test_lock_watch\.py:\d+ in slow_step(;|$)")


@pytest.mark.parametrize(("held", "warned"), [(True, 1), (False, 0)], ids=["held", "free"])
def test_a_hold_past_the_dashboards_wait_is_logged_once_naming_where_the_holder_is(
    held: bool, warned: int, caplog: pytest.LogCaptureFixture
) -> None:
    lock, taken, stop = threading.RLock(), threading.Event(), threading.Event()
    rounds: list[float] = []

    def wait() -> float:
        rounds.append(WAIT)
        if len(rounds) == ROUNDS:
            stop.set()
        return WAIT

    def slow_step() -> None:
        with lock:
            taken.set()
            deadline = time.monotonic() + 10
            while not caplog.records and time.monotonic() < deadline:
                time.sleep(WAIT)
            time.sleep(WAIT * ROUNDS)  # a watch that warned at each try would warn again by now

    with caplog.at_level(logging.WARNING, logger=lock_watch.__name__):
        if held:
            threading.Thread(target=slow_step, name="tick", daemon=True).start()
            taken.wait(10)
        lock_watch.watch(lock, wait, stop)

    said = [record.getMessage() for record in caplog.records]
    assert len(said) == warned and len(rounds) == ROUNDS
    if held:
        assert said[0].startswith("warden: its lock was held past the dashboard's 0.05 s wait, so the dashboard")
        assert HOLDER.search(said[0]), said[0]
