from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from runtime.queue import reports
from runtime.shared import environment, store
from runtime.warden import imports
from runtime.warden.context import Context
from runtime.warden.wardend import passes, ready, run_passes


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


def test_a_pass_that_works_in_a_thread_reports_its_failure_there_once_while_it_lasts(
    ctx: Context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    started: list[threading.Thread] = []

    def in_thread(name: str) -> imports.Start:
        def start(work: Callable[[], None]) -> threading.Thread:
            thread = threading.Thread(target=work, name=name)
            thread.start()
            started.append(thread)
            return thread

        return start

    def broken(_: Context) -> None:
        raise store.StoreError("reports-poll.json: not an object")

    monkeypatch.setattr(imports, "in_thread", in_thread)
    monkeypatch.setattr(reports, "poll", broken)
    pull = dict(passes(ctx.places, [], tmp_path))["reports"]
    for _ in range(2):
        pull(ctx)
        started[-1].join(10)

    assert len(started) == 2  # each pass after the last pull ended starts one
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == [
        "warden: the reports pull failed (StoreError)"
    ]


def test_the_warden_tells_systemd_it_serves_on_the_socket_systemd_names(monkeypatch: pytest.MonkeyPatch) -> None:
    name = f"software-test-{uuid.uuid4().hex}"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as systemd:
        systemd.bind("\0" + name)  # an abstract socket, which systemd names with a leading @
        systemd.settimeout(5)

        monkeypatch.setenv("NOTIFY_SOCKET", f"@{name}")
        ready(environment.take_notify_socket())

        assert systemd.recv(64) == b"READY=1"
        assert "NOTIFY_SOCKET" not in os.environ  # the programs the warden starts never send on it
