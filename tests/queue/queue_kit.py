"""A product checkout with a warden-owned queue, effective settings, green vitals, and fakes for the clock, units and
inbox."""

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
from runtime.shared import store, upgrades
from runtime.shared.models import Request, Session, Task
from runtime.shared.paths import Places
from runtime.warden import public
from runtime.warden.context import Caller, CheckPlaces, Clock, Context, StackResult
from runtime.warden.sessions import registry

ROOT = Path(__file__).resolve().parents[2]
ITEMS = (
    "G1 Sign in works. Done when it does.",
    "G2 Pages load. Done when they do.",
    "G3 Reports export. Done when they do.",
)


def goal_text(*items: str, who: str = "People.") -> str:
    listed = "".join(f"- {item}\n" for item in items)
    return f"# Goal\n\n## Who it is for\n\n{who}\n\n## The goal\n\n{listed}\n## How it looks and feels\n"


GOAL = goal_text(*ITEMS)
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
    check: Callable[[str, list[str]], tuple[int, str]] = lambda checkout, args: (0, "")
    commands: list[list[str]] = field(default_factory=list)
    stacks: list[tuple[int, list[str]]] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)
    checks: list[tuple[str, list[str]]] = field(default_factory=list)
    messages: list[tuple[str, str]] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)
    slices: list[list[str]] = field(default_factory=list)

    def start_agent(self, request_id: str, launch: dict[str, Any]) -> None:
        raise AssertionError("the queue starts no session")

    def agent_command(self, argv: list[str], cwd: str) -> tuple[int, str]:
        self.commands.append(argv)
        return self.git(argv)

    def stop_session(self, session_id: str) -> None:
        self.stopped.append(session_id)

    def message_session(self, session_id: str, text: str) -> None:
        self.messages.append((session_id, text))

    def run_stack(self, slot: int, verb: list[str], session: str, given: str = "") -> StackResult:
        self.stacks.append((slot, verb))
        self.inputs.append(given)
        return self.stack(slot, verb)

    def run_check(self, checkout: str, args: list[str]) -> tuple[int, str]:
        self.checks.append((checkout, args))
        return self.check(checkout, args)

    def set_slice(self, properties: list[str], runtime: bool) -> None:
        self.slices.append(properties)


@dataclass
class World:
    places: Places
    repo: Path
    settings: settings_mod.Settings
    clock: FakeClock
    launcher: FakeLauncher
    events: list[tuple[str, dict[str, Any]]]
    check_places: CheckPlaces = field(default_factory=CheckPlaces)

    def ctx(
        self, role: str = "orchestrator", session: str = "s-orch", agent_id: str = "", agent_kind: str = ""
    ) -> Context:
        kind = "dashboard" if role == "owner" else "agent"
        caller = Caller(kind, session if kind == "agent" else "", role, agent_id, agent_kind)
        return Context(
            self.places,
            lambda: self.settings,
            caller,
            self.clock,
            self.launcher,
            None,  # type: ignore[arg-type]  # the queue never asks GitHub
            lambda kind, data: self.events.append((kind, data)),
            check_places=self.check_places,
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

    def session(
        self, sid: str, role: str, name: str, task: str = "", worktree: str = "", agent_id: str = "", parent: str = ""
    ) -> None:
        sessions = registry.read(self.places)
        sessions[sid] = Session(
            sid,
            name,
            role,
            "impl",
            "m",
            "high",
            task,
            parent,
            worktree,
            "working",
            "2026-10-09T12:00:00Z",
            "orchestrator",
            agent_id=agent_id,
            started_as=role if agent_id else "",
        )
        registry.write(self.places, sessions)


def make_world(tmp_path: Path) -> World:
    places = Places.under(tmp_path)
    repo = tmp_path / "repo"
    (repo / "queue").mkdir(parents=True)
    (repo / "queue").chmod(0o755)
    (repo / "GOAL.md").write_text(GOAL)
    defaults = tomllib.loads((ROOT / "settings.toml").read_text())
    overrides = {"repo.path": str(repo), "repo.github": "acme/product"}
    places.install.mkdir(parents=True)
    places.defaults.write_text((ROOT / "settings.toml").read_text())
    store.write_json(upgrades.settings_path(places), overrides)
    clock = FakeClock()
    public.write_vitals(places, "green", "", 30.0, clock.iso())
    return World(places, repo, settings_mod.build(defaults, overrides), clock, FakeLauncher(), [])


def set_outcome(world: World, req: Request, outcome: str) -> None:
    world.write_raw(request_rel(req), formats.dump_request(dataclasses.replace(req, outcome=outcome)))


def plan(**parts: Any) -> bytes:
    empty = {"batch": [], "new_tasks": [], "joins": {}, "noise": {}, "parked": {}, "kept": {}}
    return json.dumps({**empty, **parts}).encode()
