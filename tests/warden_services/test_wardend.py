from __future__ import annotations

import logging

import pytest

from runtime.warden.context import Context
from runtime.warden.wardend import run_passes


def test_a_failing_pass_is_logged_with_its_class_and_the_next_still_runs(
    ctx: Context, caplog: pytest.LogCaptureFixture
) -> None:
    ran: list[str] = []

    def broken(_: Context) -> None:
        raise KeyError("a workflow detail that must not reach the log")

    run_passes(ctx, [("loop", broken), ("cost", lambda _: ran.append("cost"))])

    assert ran == ["cost"]
    [record] = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert "loop" in record.getMessage() and "KeyError" in record.getMessage()
    assert "workflow detail" not in record.getMessage()
