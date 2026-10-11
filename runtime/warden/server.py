"""The warden's sockets: warden.sock for sessions and the owner's command, owner.sock for the dashboard and the owner,
and refusals.sock where the guard reports each refusal.

A caller is known from its socket's peer process, never from its request: the peer's uid, then, for the agent user,
the registered session whose Claude process is among the peer's ancestors (Claude Code's own record of each session
process names its pid). Each stream socket answers on a bounded pool of ``factory.warden_threads`` threads.
"""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import struct
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.shared import protocol
from runtime.shared.models import RETIRED
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import registry
from runtime.warden.context import Caller
from runtime.warden.dispatch import AGENT_SOCKET, OWNER_SOCKET, Dispatcher
from runtime.warden.verbs import DASH, OWNER

log = logging.getLogger("factory.warden")
# Following PPid links ends at init; a cycle or a deeper chain than any real one means the walk is wrong.
_MAX_ANCESTRY = 64
_DATAGRAM = 65536


@dataclass(frozen=True, slots=True)
class Peers:
    """Who may connect, by uid, and where the agent user's session processes are recorded."""

    agent_uid: int
    owner_uid: int
    dashboard_uid: int
    sessions_folder: Path  # the agent user's ~/.claude/sessions
    proc: Path


def peer(conn: socket.socket) -> tuple[int, int]:
    """The peer process's pid and uid (SO_PEERCRED)."""
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _ = struct.unpack("3i", raw)
    return pid, uid


def ancestry(pid: int, proc: Path) -> list[int]:
    chain: list[int] = []
    while pid > 1 and pid not in chain and len(chain) < _MAX_ANCESTRY:
        chain.append(pid)
        try:
            status = (proc / str(pid) / "status").read_text()
        # telemetry: expected — the process ended while its ancestry was read; the chain so far is all there is
        except OSError:
            break
        pid = int(next((line.split()[1] for line in status.splitlines() if line.startswith("PPid:")), "0"))
    return chain


def _start_time(proc: Path, pid: int) -> str:
    try:
        stat = (proc / str(pid) / "stat").read_text()
    # telemetry: expected — the process ended, so its record names no live session
    except OSError:
        return ""
    # Field 22 of stat is the start time; the fields after "pid (comm)" begin at field 3.
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else ""


def session_pids(folder: Path, proc: Path) -> dict[int, str]:
    """Each live Claude session process of the agent user, by pid: its session id.

    A record whose pid now belongs to another process (the kernel's start time differs from ``procStart``) is skipped.
    """
    found: dict[int, str] = {}
    for path in folder.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        # telemetry: expected — a record is removed or rewritten as its process ends
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        pid, session_id = record.get("pid"), record.get("sessionId")
        if (
            isinstance(pid, int)
            and isinstance(session_id, str)
            and _start_time(proc, pid) == str(record.get("procStart"))
        ):
            found[pid] = session_id
    return found


def identify(places: Places, peers: Peers, socket_kind: str, pid: int, uid: int) -> Caller:
    if socket_kind == OWNER_SOCKET:
        if uid == peers.dashboard_uid:
            return Caller(kind="dashboard", role=DASH, uid=uid, pid=pid)
        if uid == peers.owner_uid:
            return Caller(kind="owner", role=OWNER, uid=uid, pid=pid)
        raise Refused("caller", "this socket answers only the dashboard and the owner")
    if uid == peers.owner_uid:
        return Caller(kind="owner", role=OWNER, uid=uid, pid=pid)
    if uid != peers.agent_uid:
        raise Refused("caller", "this socket answers factory sessions and the owner's own command")
    chain = set(ancestry(pid, peers.proc))
    live = session_pids(peers.sessions_folder, peers.proc)
    sessions = registry.read(places)
    for session_pid, session_id in live.items():
        session = sessions.get(session_id)
        if session_pid in chain and session is not None:
            if session.state == RETIRED:
                raise Refused("retired", f"{session.name} is retired and can never act again")
            return Caller(kind="agent", session=session.id, role=session.role, uid=uid, pid=pid,
                          worktree=session.worktree)  # fmt: skip
    return Caller(kind="agent", uid=uid, pid=pid)


class _PooledServer(socketserver.UnixStreamServer):
    """A Unix stream server whose requests run on a fixed pool of threads."""

    def __init__(self, path: Path, threads: int, answer: Callable[[int, int, bytes], dict[str, Any]],
                 read_seconds: Callable[[], int]) -> None:  # fmt: skip
        self.answer, self.read_seconds = answer, read_seconds
        self.pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix=path.stem)
        super().__init__(str(path), _Request)

    def process_request(self, request: Any, client_address: Any) -> None:
        self.pool.submit(self._work, request, client_address)

    def _work(self, request: Any, client_address: Any) -> None:
        try:
            self.finish_request(request, client_address)
        except Exception as exc:
            where = Path(str(self.server_address)).name
            log.exception("warden: a request on %s failed (%s)", where, type(exc).__name__)
        finally:
            self.shutdown_request(request)


class _Request(socketserver.StreamRequestHandler):
    server: _PooledServer

    def handle(self) -> None:
        self.connection.settimeout(self.server.read_seconds())
        try:
            line = self.rfile.readline(protocol.MAX_LINE + 1)
            pid, uid = peer(self.connection)
            self.wfile.write(protocol.answer(self.server.answer(pid, uid, line)))
        # telemetry: expected — the caller went away or sent nothing in time; it has no one to answer
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            return


def _listen(path: Path, threads: int, answer: Callable[[int, int, bytes], dict[str, Any]],
            read_seconds: Callable[[], int]) -> _PooledServer:  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = _PooledServer(path, threads, answer, read_seconds)
    # Every user may connect; the peer's uid decides what it may ask.
    path.chmod(0o666)
    threading.Thread(target=server.serve_forever, name=path.stem, daemon=True).start()
    return server


def serve(places: Places, peers: Peers, dispatcher: Dispatcher, threads: int) -> list[_PooledServer]:
    """Start answering on warden.sock and owner.sock; returns the servers so a test can shut them down."""
    settings = dispatcher.doors.settings

    def read_seconds() -> int:
        return settings().get_int("watch.socket_read_seconds")

    def on(socket_kind: str) -> Callable[[int, int, bytes], dict[str, Any]]:
        def answer(pid: int, uid: int, line: bytes) -> dict[str, Any]:
            try:
                caller = identify(places, peers, socket_kind, pid, uid)
            except Refused as exc:
                return protocol.refused(exc.rule, exc.reason, exc.detail)
            return dispatcher.answer(caller, socket_kind, line)

        return answer

    return [
        _listen(places.socket, threads, on(AGENT_SOCKET), read_seconds),
        _listen(places.owner_socket, threads, on(OWNER_SOCKET), read_seconds),
    ]


def serve_refusals(path: Path, on_refusal: Callable[[dict[str, Any]], None]) -> socket.socket:
    """Receive the guard's refusal datagrams; one that is not a JSON object is dropped."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    path.chmod(0o666)

    def receive() -> None:
        while True:
            data = sock.recv(_DATAGRAM)
            try:
                line = json.loads(data)
            # telemetry: expected — only the guard sends here, so a datagram that is not JSON is noise
            except ValueError:
                continue
            if not isinstance(line, dict):
                continue
            try:
                on_refusal(line)
            except Exception as exc:
                log.exception("warden: recording a refusal failed (%s)", type(exc).__name__)

    threading.Thread(target=receive, name="refusals", daemon=True).start()
    return sock
