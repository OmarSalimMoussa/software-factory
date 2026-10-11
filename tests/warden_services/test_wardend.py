from __future__ import annotations

import logging
import socket
import uuid

import pytest

from runtime.warden.context import Context
from runtime.warden.wardend import ready, run_passes


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


def test_the_warden_tells_systemd_it_serves_on_the_socket_systemd_names() -> None:
    name = f"software-test-{uuid.uuid4().hex}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as systemd:
        systemd.bind("\0" + name)  # an abstract socket, which systemd names with a leading @
        systemd.settimeout(5)

        ready({"NOTIFY_SOCKET": f"@{name}"})

        assert systemd.recv(64) == b"READY=1"
