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

from dashboard_seed import PREVIOUS, PROD, STAGING, iso

from runtime.shared import settings as settings_module
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.protocol import MAX_LINE
from runtime.warden import backstop, codes, launch
from runtime.warden.context import Caller, Clock, StackResult
from runtime.warden.dispatch import OWNER_SOCKET, Dispatcher, Doors, resolve
from runtime.warden.verbs import DASH, VERBS

DASHBOARD = Caller(kind="dashboard", role=DASH)
_numbers = itertools.count()


class FixedClock(Clock):
    def __init__(self, now: float) -> None:
        self.t = now

    def now(self) -> float:
        return self.t


class Launcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def start_agent(self, request_id: str, launch: dict[str, Any]) -> None:
        self.calls.append(("start", request_id))

    def agent_command(self, argv: list[str], cwd: str) -> tuple[int, str]:
        self.calls.append(("command", argv))
        return 0, ""

    def stop_session(self, session_id: str) -> None:
        self.calls.append(("stop", session_id))

    def run_stack(self, slot: int, verb: list[str], session: str) -> StackResult:
        return StackResult(exit=0, tail="", log="stack.log")


class GitHub:
    def __init__(self, now: Callable[[], float]) -> None:
        self.now = now

    def compare(self, base: str, head: str) -> list[dict[str, Any]]:
        return []

    def dispatch_promote(self, sha: str) -> str:
        return "1"

    def run(self, run_id: str) -> dict[str, Any]:
        return {"id": run_id, "status": "completed", "conclusion": "success", "url": "https://github.invalid/run"}

    def deployment(self, environment: str) -> dict[str, Any]:
        sha = STAGING if environment == "staging" else PROD
        current = {"sha": sha, "at": iso(self.now() - 3 * 3600), "url": "https://api.github.invalid/d/1"}
        previous = {"sha": PREVIOUS, "at": iso(self.now() - 2 * 86400), "url": "https://api.github.invalid/d/0"}
        return {"current": current, "previous": previous}

    def reachable(self) -> bool:
        return True


class FakeWarden:
    """Serves owner.sock until stopped; ``accept`` is a code every coded verb takes, for the browser drives."""

    def __init__(self, places: Places, clock: Clock, accept: str | None = None, github: Any = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:  # fmt: skip
        self.places, self.clock = places, clock
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.launcher = Launcher()

        def post(kind: str, data: dict[str, Any]) -> None:
            self.events.append((kind, data))
            if on_event is not None:
                on_event(kind, data)

        self.doors = Doors(places, self._settings, clock, self.launcher, github or GitHub(clock.now), post,
                           threading.RLock())  # fmt: skip
        owner = {name: verb for name, verb in VERBS.items() if verb.owner_socket}
        self.dispatcher = Dispatcher(self.doors, resolve(owner), owner)
        self.server: socketserver.UnixStreamServer | None = None
        # An abstract socket: a temporary folder's path is often longer than a socket path may be.
        self.address = Path(f"\0dashboard-test-{os.getpid()}-{next(_numbers)}")
        # No Claude session runs on a test machine, and its agent user does not exist here.
        backstop.processes = dict
        launch.agent_home = lambda: places.state / "agent-home"
        if accept is not None:
            real = codes.check_code

            def check(ctx: Any, code: object, *, lock_exempt: bool = False) -> None:
                if code == accept and codes.locked_until(ctx.places, ctx.clock.now()) is None:
                    store.write_json(ctx.places.code_lock, {"last_step": None, "wrong": [], "locked_until": None})
                    return
                real(ctx, code, lock_exempt=lock_exempt)

            codes.check_code = check

    def _settings(self) -> settings_module.Settings:
        return settings_module.load(self.places.defaults, self.places.overrides)

    def start(self) -> FakeWarden:
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
