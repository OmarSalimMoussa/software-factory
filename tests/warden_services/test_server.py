from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest

from runtime.shared import protocol
from runtime.shared.models import DEPRIORITIZED, RETIRED, WORKING, Session
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import registry, server
from runtime.warden.dispatch import AGENT_SOCKET, OWNER_SOCKET, Dispatcher, Doors
from runtime.warden.verbs import ANY, Verb

AGENT, OWNER, DASHBOARD, STRANGER = 1001, 1000, 1002, 1003
CLAUDE, SHELL, CLI = 400, 500, 600


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

    caller = server.identify(places, peers, AGENT_SOCKET, CLI, AGENT)

    assert (caller.kind, caller.session, caller.role, caller.worktree) == ("agent", "s1", "implementer", "/w/s1")


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
        role = server.identify(places, peers, socket_kind, CLI, uid).role
    except Refused as exc:
        role = exc.rule
    assert role == expect


def test_a_process_whose_record_names_a_reused_pid_is_no_session(places: Places, peers: server.Peers) -> None:
    registry.write(places, {"s1": session("s1")})
    (peers.sessions_folder / "400.json").write_text(json.dumps({"pid": CLAUDE, "sessionId": "s1", "procStart": "9"}))

    assert server.identify(places, peers, AGENT_SOCKET, CLI, AGENT).role == ""


def test_the_socket_answers_one_json_line_per_request(places: Places, doors: Doors, tmp_path: Path) -> None:
    def status(ctx: Any, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
        return {"who": ctx.caller.kind}

    dispatcher = Dispatcher(doors, {"status": status}, {"status": Verb("x:y", ANY)})
    me = server.Peers(agent_uid=-2, owner_uid=os.getuid(), dashboard_uid=-3, sessions_folder=tmp_path, proc=tmp_path)
    servers = server.serve(places, me, dispatcher, threads=2)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(str(places.socket))
            conn.sendall(protocol.request("status", {"as": "orchestrator"}))
            reply = json.loads(conn.makefile("rb").readline())
    finally:
        for running in servers:
            running.shutdown()
            running.server_close()
    assert reply == {"who": "owner", "ok": True}
