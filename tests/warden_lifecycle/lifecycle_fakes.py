from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from runtime.queue import approvals, route, statuses
from runtime.shared import settings as settings_mod
from runtime.shared import store
from runtime.shared.claude_accounts import UNIT_TOKEN
from runtime.shared.models import PENDING, WORKING, Session, Task
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.system import layout
from runtime.warden.context import Caller, Clock, Context, GitHub, StackResult
from runtime.warden.sessions import registry, session_settings
from runtime.warden.sessions.backstop import Proc

ROOT = Path(__file__).resolve().parents[2]
REMOTE = "git@github.com:acme/product.git"


def at_once(work: Callable[[], None]) -> None:
    """Settles the sessions pass's benches at once, so a test reads the result as the pass returns."""
    work()


class FakeClock(Clock):
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 9, 12, 0, tzinfo=UTC).timestamp()

    def now(self) -> float:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.at += seconds


class FakeLauncher:
    """Records what the warden asked for; answers commands the way claude and git would."""

    def __init__(self, procs: dict[str, Proc], places: Places) -> None:
        self.procs = procs  # a stop ends a process, a respawn starts one
        self.places = places
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.stopped: list[str] = []
        self.commands: list[list[str]] = []
        self.stacks: list[tuple[int, list[str]]] = []
        self.remote = REMOTE
        self.lost = False
        self.stays_down = False
        self.resumed: list[str] = []
        self.messages: list[tuple[str, str]] = []
        self.restarts = 0
        self.upgrades: list[str] = []
        self.on_restart: Callable[[], None] = lambda: None  # what else happens while the daemon restarts
        # The token the daemon signed in with at its last restart, and how each token's first call answers.
        self.held: str | None = None
        self.answers: dict[str, str | None] = {}

    def start_agent(self, request_id: str, launch: dict[str, Any]) -> None:
        self.started.append((request_id, launch))
        if launch.get("resume") and not self.stays_down:
            copy = f"copy-{launch['resume']}"
            self.procs[copy] = Proc(pid=6000 + len(self.started), session=copy, name=launch["name"], chat_link="")

    def agent_command(self, argv: list[str], cwd: str) -> tuple[int, str]:
        self.commands.append(argv)
        if argv[:2] == ["claude", "respawn"]:
            if self.lost:
                return 1, f"No background session {argv[2]}"
            [sid] = [sid for sid in registry.read(self.places) if sid.startswith(argv[2])]
            self.resumed.append(sid)
            if not self.stays_down:
                self.procs[sid] = Proc(pid=5000 + len(self.commands), session=sid, name="", chat_link="")
            return 0, ""
        if "remote" in argv:
            return 0, self.remote + "\n"
        if "rev-parse" in argv:
            return 0, "worktree-branch\n"
        if argv[1:3] == ["worktree", "remove"]:
            return 1, "fatal: it contains modified or untracked files"
        return 0, ""

    def message_session(self, session_id: str, text: str) -> None:
        self.messages.append((session_id, text))

    def stop_session(self, session_id: str) -> None:
        self.stopped.append(session_id)
        self.procs.pop(session_id, None)

    def run_stack(self, slot: int, verb: list[str], session: str) -> StackResult:
        self.stacks.append((slot, verb))
        return StackResult(0, "", "stack.log")

    def start_upgrade(self, commit: str) -> None:
        self.upgrades.append(commit)

    def restart_daemon(self) -> None:
        self.restarts += 1
        self.procs.clear()  # stopping the daemon stops every session it hosts
        self.held = self._loaded()
        self.on_restart()

    def _loaded(self) -> str | None:
        unit = layout.secrets(self.places.state) / UNIT_TOKEN
        return unit.read_text().strip() if unit.exists() else None

    def signed_in(self) -> str | None:
        return self.held if self.restarts else self._loaded()

    def answered(self, session_id: str, since: float) -> str | None:
        """As the real one, which reads the session's own transcript: only a session that runs answers."""
        held = self.signed_in()
        return self.answers.get(held, "ok") if held is not None and session_id in self.procs else None


class Queue:
    """The tasks, in memory; status moves go through the real status table."""

    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.moves: list[tuple[str, str, str]] = []

    def add(self, task_id: str, **fields: Any) -> Task:
        base: dict[str, Any] = dict(title=f"Task {task_id}", requests=["r-2026-10-09-0001"], goal_item="G1",
                                    severity="major", surfaces=["api"], status="approved",
                                    created="2026-10-01T00:00:00Z", body="", run=1, slices=1,
                                    cause="app/notes.py:41 reads the wrong field", owner_class="none",
                                    why="It was proposed.", picture="t.svg")  # fmt: skip
        task = Task(id=task_id, **{**base, **fields})
        self.tasks[task_id] = task
        return task

    def read(self, ctx: Context, task_id: str) -> Task:
        if task_id not in self.tasks:
            raise Refused("task", f"no task {task_id}")
        return self.tasks[task_id]

    def move(self, ctx: Context, task_id: str, to: str, event: str, by: str = statuses.WARDEN) -> None:
        self.tasks[task_id] = statuses.move(self.tasks[task_id], to, event, by)
        self.moves.append((task_id, to, event))


@dataclasses.dataclass
class World:
    places: Places
    settings: settings_mod.Settings
    clock: FakeClock
    launcher: FakeLauncher
    queue: Queue
    procs: dict[str, Proc]
    events: list[tuple[str, dict[str, Any]]]
    daemon: float | None = None  # when the agent's Claude daemon started; set a day before the clock's start

    def ctx(self, role: str = "orchestrator", kind: str = "agent", session: str = "") -> Context:
        caller = Caller(kind=kind, role=role, session=session)
        return Context(self.places, lambda: self.settings, caller, self.clock, cast(Any, self.launcher),
                       cast(GitHub, None), lambda kind, data: self.events.append((kind, data)), self.lock)  # fmt: skip

    def owner(self) -> Context:
        return self.ctx(role="dashboard", kind="dashboard")

    def seat(self, sid: str, role: str = "implementer", task: str = "", **fields: Any) -> Session:
        sessions = registry.read(self.places)
        base: dict[str, Any] = dict(name=f"{role}-{sid}", family="impl", model="m", effort="high", task=task,
                                    parent="", worktree=f"/w/{sid}", state=WORKING,
                                    started="2026-10-09T11:00:00Z", started_by="orchestrator",
                                    since="2026-10-09T11:00:00Z")  # fmt: skip
        if not fields.get("parent") and "definitions" not in fields:
            agents = session_settings.agents(self.settings, self.places, role, base["model"], base["effort"])
            base["definitions"] = session_settings.fingerprint(agents)
        session = Session(id=sid, role=role, **{**base, **fields})
        sessions[sid] = session
        registry.write(self.places, sessions)
        # The backstop's line binding it to its start's pending id, which names its sandbox settings.
        store.append_jsonl(
            self.places.history,
            {
                "at": session.started,
                "session": sid,
                "was": f"{PENDING}{sid}",
                "name": session.name,
                "by": "warden",
                "why": "bound",
            },
        )
        return session

    def session(self, sid: str) -> Session:
        return registry.read(self.places)[sid]

    def run(self, sid: str) -> None:
        self.procs[sid] = Proc(pid=4000 + len(self.procs), session=sid, name=self.session(sid).name, chat_link="")

    def vitals(self, level: str, why: str = "") -> None:
        store.write_json(self.places.vitals, {"at": "2026-10-09T12:00:00Z", "level": level, "why": why, "readings": []})

    def later(self, seconds: float) -> None:
        self.clock.at += seconds

    def approve(self, task_id: str = "t-0001", **fields: Any) -> Task:
        task = self.queue.add(task_id, **fields)
        model = self.settings.model("implementer_small" if task.run == 1 else "implementer_large")[0]
        approvals.record(self.places, task, model, "2026-10-09T11:00:00Z", route.route(task, self.settings))
        return task

    def cap(self, n: int) -> None:
        self.settings = self.settings.with_changes({"caps.implementers": n})

    lock: threading.RLock = dataclasses.field(default_factory=threading.RLock)
