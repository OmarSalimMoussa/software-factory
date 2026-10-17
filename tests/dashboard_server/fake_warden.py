"""A warden for the dashboard's tests: the real dispatcher and owner verbs on a seeded state folder, answering on
owner.sock as it answers the dashboard, with fakes only for what reaches outside (units, GitHub, the clock)."""

from __future__ import annotations

import itertools
import json
import os
import socketserver
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.shared import settings as settings_module
from runtime.shared import store
from runtime.shared.claude_accounts import UNIT_TOKEN
from runtime.shared.paths import Places
from runtime.shared.protocol import MAX_LINE
from runtime.slots import leases
from runtime.system import layout
from runtime.warden.context import Caller, CheckPlaces, Clock, StackResult
from runtime.warden.dispatch import OWNER_SOCKET, Dispatcher, Doors, resolve
from runtime.warden.owner import codes
from runtime.warden.sessions import backstop, launch, respawn
from runtime.warden.verbs import DASH, VERBS

DASHBOARD = Caller(kind="dashboard", role=DASH)
_numbers = itertools.count()


class FixedClock(Clock):
    def __init__(self, now: float) -> None:
        self.t = now

    def now(self) -> float:
        return self.t


class Launcher:
    def __init__(self, places: Places) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.places = places
        self.deploy: Callable[[str], tuple[int, str]] = lambda sha: (0, "")  # prod's build and deploy

    def start_agent(self, request_id: str, launch: dict[str, Any]) -> None:
        self.calls.append(("start", request_id))

    def agent_command(self, argv: list[str], cwd: str) -> tuple[int, str]:
        self.calls.append(("command", argv))
        return 0, ""

    def stop_session(self, session_id: str) -> None:
        self.calls.append(("stop", session_id))

    def message_session(self, session_id: str, text: str) -> None:
        self.calls.append(("message", (session_id, text)))

    def run_stack(self, slot: int, verb: list[str], session: str, given: str = "") -> StackResult:
        """Slot 0's sign-in prints a one-time link on its own address; every other verb prints nothing."""
        settings = settings_module.load(self.places.defaults, self.places.overrides)
        link = f"{leases.origin(settings, slot)}/sign-in/once"
        return StackResult(exit=0, tail=link if verb[:1] == ["sign-in"] else "", log="stack.log")

    def start_upgrade(self, commit: str) -> None:
        self.calls.append(("upgrade", commit))

    def set_slice(self, properties: list[str], runtime: bool) -> None:
        self.calls.append(("slice", properties))

    def deploy_prod(self, sha: str, back: bool) -> tuple[int, str]:
        self.calls.append(("deploy", sha))
        return self.deploy(sha)

    def restart_daemon(self) -> None:
        self.calls.append(("restart", None))

    def signed_in(self) -> str | None:
        unit = layout.secrets(self.places.state) / UNIT_TOKEN
        return unit.read_text().strip() if unit.exists() else None

    def answered(self, session_id: str, since: float) -> str | None:
        return "ok"


class GitHub:
    def reachable(self) -> bool:
        return True


class FakeWarden:
    """Serves owner.sock until stopped; ``accept`` is a code every coded verb takes, for the browser drives."""

    def __init__(self, places: Places, clock: Clock, accept: str | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:  # fmt: skip
        self.places, self.clock = places, clock
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.launcher = Launcher(places)

        def post(kind: str, data: dict[str, Any]) -> None:
            self.events.append((kind, data))
            if on_event is not None:
                on_event(kind, data)

        self.doors = Doors(places, self._settings, clock, self.launcher, GitHub(), post,
                           threading.RLock(), CheckPlaces())  # fmt: skip
        owner = {name: verb for name, verb in VERBS.items() if verb.owner_socket}
        self.dispatcher = Dispatcher(self.doors, resolve(owner), owner)
        self.server: socketserver.UnixStreamServer | None = None
        # An abstract socket: a temporary folder's path is often longer than a socket path may be.
        self.address = Path(f"\0dashboard-test-{os.getpid()}-{next(_numbers)}")
        self.accept = accept
        self._real: tuple[Any, ...] | None = None

    def _patch(self) -> None:
        """No Claude session runs on a test machine, and its agent user does not exist here: while this warden
        answers, the module functions that look for them see the seeded agent home instead, and a resume is recorded
        as a start is. stop() puts the real ones back, so a later test in the same run never inherits these."""
        self._real = (backstop.processes, launch.agent_home, respawn.resume, codes.check_code)
        places, accept, real, calls = self.places, self.accept, codes.check_code, self.launcher.calls
        backstop.processes = dict
        launch.agent_home = lambda: places.state / "agent-home"
        respawn.resume = lambda ctx, s, message: calls.append(("resume", (s.id, message))) or s.id
        if accept is not None:

            def check(ctx: Any, code: object) -> None:
                if code == accept and codes.locked_until(ctx.places, ctx.clock.now()) is None:
                    store.write_json(ctx.places.code_lock, {"last_step": None, "wrong": [], "locked_until": None})
                    return
                real(ctx, code)

            codes.check_code = check

    def _settings(self) -> settings_module.Settings:
        return settings_module.load(self.places.defaults, self.places.overrides)

    def start(self) -> FakeWarden:
        self._patch()
        dispatcher = self.dispatcher

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                line = self.rfile.readline(MAX_LINE + 1)
                answer = dispatcher.answer(DASHBOARD, OWNER_SOCKET, line)
                self.wfile.write((json.dumps(answer) + "\n").encode())

        self.server = socketserver.ThreadingUnixStreamServer(str(self.address), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self._real is not None:
            backstop.processes, launch.agent_home, respawn.resume, codes.check_code = self._real
            self._real = None
