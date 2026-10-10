"""A product checkout with a warden-owned queue, effective settings, and fakes for the clock, units and inbox."""

from __future__ import annotations

import dataclasses
import json
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.queue import formats, intake, tasks
from runtime.queue.book import Queue, request_rel
from runtime.shared import settings as settings_mod
from runtime.shared.models import Request, Session, Task
from runtime.shared.paths import Places
from runtime.warden import registry
from runtime.warden.context import Caller, Clock, Context, StackResult

ROOT = Path(__file__).resolve().parents[2]
GOAL = (
    "# Goal\n\n## Who it is for\n\nPeople.\n\n## The goal\n\n- G1 Sign in works. Done when it does.\n"
    "- G2 Pages load. Done when they do.\n- G3 Reports export. Done when they do.\n\n## How it looks and feels\n"
)
PICTURE = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="4" height="4"/></svg>'


class FakeClock(Clock):
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 9, 12, 0, tzinfo=UTC).timestamp()

    def now(self) -> float:
        return self.at


@dataclass
class FakeLauncher:
    """Answers git and scripts/dev the way a test scripts them; records every call."""

    git: Callable[[list[str]], tuple[int, str]] = lambda argv: (0, "")
    stack: Callable[[int, list[str]], StackResult] = lambda n, verb: StackResult(0, "", "")
    commands: list[list[str]] = field(default_factory=list)
    stacks: list[tuple[int, list[str]]] = field(default_factory=list)

    def start_agent(self, request_id: str, launch: dict[str, Any]) -> None:
        raise AssertionError("the queue starts no session")

    def agent_command(self, argv: list[str], cwd: str) -> tuple[int, str]:
        self.commands.append(argv)
        return self.git(argv)

    def stop_session(self, session_id: str) -> None:
        raise AssertionError("the queue stops no session")

    def run_stack(self, slot: int, verb: list[str], session: str) -> StackResult:
        self.stacks.append((slot, verb))
        return self.stack(slot, verb)


@dataclass
class World:
    places: Places
    repo: Path
    settings: settings_mod.Settings
    clock: FakeClock
    launcher: FakeLauncher
    events: list[tuple[str, dict[str, Any]]]

    def ctx(self, role: str = "orchestrator", session: str = "s-orch", agent_id: str = "") -> Context:
        kind = "dashboard" if role == "owner" else "agent"
        caller = Caller(kind, session if kind == "agent" else "", role, agent_id)
        return Context(
            self.places,
            lambda: self.settings,
            caller,
            self.clock,
            self.launcher,
            None,  # type: ignore[arg-type]
            lambda kind, data: self.events.append((kind, data)),
        )

    def owner(self) -> Context:
        return self.ctx("owner")

    def queue(self) -> Queue:
        return Queue.of(self.ctx())

    def with_settings(self, **changes: Any) -> None:
        self.settings = self.settings.with_changes({k.replace("__", "."): v for k, v in changes.items()})

    def request(self, inbox: str = "agents", sender: str = "implementer", severity: str = "normal") -> Request:
        ctx, queue = self.ctx(), self.queue()
        with queue.lock():
            return intake.file_request(ctx, queue, inbox, sender, severity, "the sign-in page", "It loops.")

    def task(self, **facts: Any) -> Task:
        """A queued task made through the fast lane from a fresh request."""
        req = self.request()
        args = {
            "requests": [req.id],
            "title": "Sign-in loops",
            "goal_item": "G1",
            "severity": "major",
            "surfaces": ["ui"],
            "cause": "web/auth.py:41",
            "slices": 1,
            "owner_class": "none",
            **facts,
        }
        return tasks.new_task(self.ctx(), args, "Fix the loop.")

    def write_raw(self, rel: str, text: str) -> None:
        self.queue().files.write(rel, text.encode())

    def session(self, sid: str, role: str, name: str, task: str = "", worktree: str = "", agent_id: str = "") -> None:
        sessions = registry.read(self.places)
        sessions[sid] = Session(
            sid,
            name,
            role,
            "impl",
            "m",
            "high",
            task,
            "",
            worktree,
            None,
            "working",
            "2026-10-09T12:00:00Z",
            "orchestrator",
            agent_id=agent_id,
        )
        registry.write(self.places, sessions)


def make_world(tmp_path: Path) -> World:
    places = Places.under(tmp_path)
    repo = tmp_path / "repo"
    (repo / "queue").mkdir(parents=True)
    (repo / "queue").chmod(0o755)
    (repo / "GOAL.md").write_text(GOAL)
    defaults = tomllib.loads((ROOT / "settings.toml").read_text())
    overrides = {
        "repo.path": str(repo),
        "repo.github": "acme/product",
        "repo.staging_health": "https://s/health",
        "repo.prod_health": "https://p/health",
    }
    return World(places, repo, settings_mod.build(defaults, overrides), FakeClock(), FakeLauncher(), [])


def set_outcome(world: World, req: Request, outcome: str) -> None:
    world.write_raw(request_rel(req), formats.dump_request(dataclasses.replace(req, outcome=outcome)))


def plan(**parts: Any) -> bytes:
    return json.dumps({"batch": [], "new_tasks": [], "joins": {}, "noise": {}, "parked": {}, **parts}).encode()
