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
from runtime.shared.models import RETIRED, Session
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden.context import Caller, Clock
from runtime.warden.dispatch import AGENT_SOCKET, OWNER_SOCKET, Dispatcher
from runtime.warden.sessions import backstop, registry
from runtime.warden.sessions.backstop import Proc
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


Bind = Callable[[dict[str, Proc]], dict[str, Session]]


def binder(places: Places, lock: threading.RLock, clock: Clock) -> Bind:
    """The registry once every pending start whose session now runs is bound to it, under the warden's lock."""

    def bind_now(procs: dict[str, Proc]) -> dict[str, Session]:
        with lock:
            sessions = registry.read(places)
            backstop.bind_running(places, clock, sessions, procs)
            return sessions

    return bind_now


def identify(places: Places, peers: Peers, socket_kind: str, pid: int, uid: int, bind_now: Bind) -> Caller:
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
    procs = backstop.read_processes(peers.sessions_folder, peers.proc)
    by_pid = {p.pid: p.session for p in procs.values()}
    mine = [by_pid[p] for p in ancestry(pid, peers.proc) if p in by_pid]
    sessions = registry.read(places)
    if any(sid not in sessions for sid in mine):
        # A start is bound to its session on the backstop's next tick; a session that asks before then is bound now.
        sessions = bind_now(procs)
    session = next((sessions[sid] for sid in mine if sid in sessions), None)
    if session is None:
        return Caller(kind="agent", uid=uid, pid=pid)
    if session.state == RETIRED:
        raise Refused("retired", f"{session.name} is retired and can never act again")
    return Caller(kind="agent", session=session.id, role=session.role, uid=uid, pid=pid, worktree=session.worktree)


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
            reply = protocol.answer(self.server.answer(pid, uid, line))
            if len(reply) > protocol.MAX_LINE:
                # A caller reads at most MAX_LINE bytes of a line: a longer answer would reach it cut short.
                where = Path(str(self.server.server_address)).name
                log.warning("warden: an answer of %d bytes on %s is too large to send", len(reply), where)
                reply = protocol.answer(protocol.refused("protocol", "the answer is too large to send"))
            self.wfile.write(reply)
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
    bind_now = binder(places, dispatcher.doors.lock, dispatcher.doors.clock)

    def read_seconds() -> int:
        return settings().get_int("watch.socket_read_seconds")

    def on(socket_kind: str) -> Callable[[int, int, bytes], dict[str, Any]]:
        def answer(pid: int, uid: int, line: bytes) -> dict[str, Any]:
            try:
                caller = identify(places, peers, socket_kind, pid, uid, bind_now)
            # telemetry: surfaced — the caller's answer is `refused`, with the rule and the reason
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
