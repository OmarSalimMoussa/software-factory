from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from runtime.shared import protocol
from runtime.shared.models import DEPRIORITIZED, RETIRED, WORKING, Session
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import server
from runtime.warden.context import Clock
from runtime.warden.dispatch import AGENT_SOCKET, OWNER_SOCKET, Dispatcher, Doors
from runtime.warden.sessions import registry
from runtime.warden.verbs import ANY, Verb

AGENT, OWNER, DASHBOARD, STRANGER = 1001, 1000, 1002, 1003
CLAUDE, SHELL, CLI = 400, 500, 600


def binding(places: Places) -> server.Bind:
    return server.binder(places, threading.RLock(), Clock())


def session(sid: str, state: str = WORKING) -> Session:
    return Session(id=sid, name=f"impl-{sid}", role="implementer", family="impl", model="m", effort="high",
                   task="t-0001", parent="", worktree="/w/" + sid, state=state, started="2026-10-09T10:00:00Z",
                   started_by="orchestrator")  # fmt: skip


@pytest.fixture
def peers(tmp_path: Path) -> server.Peers:
    proc, folder = tmp_path / "proc", tmp_path / "sessions"
    # A claude process, its shell, and the factory command the shell ran.
    for pid, ppid in ((CLAUDE, 1), (SHELL, CLAUDE), (CLI, SHELL)):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "status").write_text(f"Name:\tx\nPPid:\t{ppid}\n")
        (proc / str(pid) / "stat").write_text(f"{pid} (x) S {ppid} " + " ".join(str(n) for n in range(17)) + " 777 0")
    folder.mkdir()
    (folder / "400.json").write_text(json.dumps({"pid": CLAUDE, "sessionId": "s1", "procStart": "777"}))
    return server.Peers(agent_uid=AGENT, owner_uid=OWNER, dashboard_uid=DASHBOARD, sessions_folder=folder, proc=proc)


def test_an_agent_caller_is_the_registered_session_among_its_ancestors(places: Places, peers: server.Peers) -> None:
    registry.write(places, {"s1": session("s1")})

    caller = server.identify(places, peers, AGENT_SOCKET, CLI, AGENT, binding(places))

    assert (caller.kind, caller.session, caller.role, caller.worktree) == ("agent", "s1", "implementer", "/w/s1")


def test_a_session_that_asks_before_the_backstop_bound_its_start_is_bound_then(
    places: Places, peers: server.Peers
) -> None:
    # A spare the daemon kept warm runs a new session's first command within a second: before the backstop's tick.
    registry.write(places, {"pending-1": session("pending-1")})
    (peers.sessions_folder / "400.json").write_text(
        json.dumps({"pid": CLAUDE, "sessionId": "s1", "procStart": "777", "name": "impl-pending-1"})
    )

    caller = server.identify(places, peers, AGENT_SOCKET, CLI, AGENT, binding(places))

    assert (caller.session, caller.role) == ("s1", "implementer") and list(registry.read(places)) == ["s1"]
    assert '"was": "pending-1"' in places.history.read_text()


@pytest.mark.parametrize(
    ("socket_kind", "uid", "state", "expect"),
    [
        (OWNER_SOCKET, AGENT, WORKING, "caller"),
        (OWNER_SOCKET, STRANGER, WORKING, "caller"),
        (AGENT_SOCKET, DASHBOARD, WORKING, "caller"),
        (AGENT_SOCKET, AGENT, RETIRED, "retired"),
        (AGENT_SOCKET, AGENT, DEPRIORITIZED, "implementer"),
        (OWNER_SOCKET, DASHBOARD, WORKING, "dashboard"),
        (AGENT_SOCKET, OWNER, WORKING, "owner"),
    ],
)
def test_the_peer_uid_decides_who_may_ask_on_each_socket(
    places: Places, peers: server.Peers, socket_kind: str, uid: int, state: str, expect: str
) -> None:
    registry.write(places, {"s1": session("s1", state)})
    try:
        role = server.identify(places, peers, socket_kind, CLI, uid, binding(places)).role
    except Refused as exc:
        role = exc.rule
    assert role == expect


def test_a_process_whose_record_names_a_reused_pid_is_no_session(places: Places, peers: server.Peers) -> None:
    registry.write(places, {"s1": session("s1")})
    (peers.sessions_folder / "400.json").write_text(json.dumps({"pid": CLAUDE, "sessionId": "s1", "procStart": "9"}))

    assert server.identify(places, peers, AGENT_SOCKET, CLI, AGENT, binding(places)).role == ""


def _ask(places: Places, doors: Doors, tmp_path: Path, run: Any) -> dict[str, Any]:
    """One request on the agent socket to a warden whose only verb is ``run``, asked as the owner."""
    dispatcher = Dispatcher(doors, {"status": run}, {"status": Verb("x:y", ANY)})
    me = server.Peers(agent_uid=-2, owner_uid=os.getuid(), dashboard_uid=-3, sessions_folder=tmp_path, proc=tmp_path)
    servers = server.serve(places, me, dispatcher, threads=2)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(str(places.socket))
            conn.sendall(protocol.request("status", {"as": "orchestrator"}))
            reply: dict[str, Any] = json.loads(conn.makefile("rb").readline())
            return reply
    finally:
        for running in servers:
            running.shutdown()
            running.server_close()


def test_the_socket_answers_one_json_line_per_request(places: Places, doors: Doors, tmp_path: Path) -> None:
    reply = _ask(places, doors, tmp_path, lambda ctx, args, data: {"who": ctx.caller.kind})

    assert reply == {"who": "owner", "ok": True}


def test_an_answer_past_the_line_limit_is_refused_and_logged_never_sent_cut_short(
    places: Places, doors: Doors, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(protocol, "MAX_LINE", 1000)

    reply = _ask(places, doors, tmp_path, lambda ctx, args, data: {"blob": "x" * 2000})

    assert (reply["ok"], reply["refused"]) == (False, "protocol")
    assert "too large to send" in caplog.text
