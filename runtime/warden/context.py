"""What every warden handler receives: who is asking, the effective settings, the clock, and the warden's doors to
the outside (launching units, GitHub, the orchestrator's inbox).

Handlers take a Context, never build their own clients, so a test or the rehearsal passes fakes.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from runtime.shared.paths import Places
from runtime.shared.settings import Settings


@dataclass(frozen=True, slots=True)
class Caller:
    """Who sent a request, known from the socket's peer process, never from the request."""

    kind: str  # agent, owner, dashboard
    session: str = ""  # the registered session among the caller's ancestors
    role: str = ""  # orchestrator, implementer, designer, owner, dashboard
    agent_id: str = ""  # set when a subagent's command sent it
    agent_kind: str = ""  # that subagent's kind, from the registry
    uid: int = -1
    pid: int = -1
    worktree: str = ""


@dataclass(frozen=True, slots=True)
class StackResult:
    exit: int
    tail: str  # the last slots.log_tail_lines lines, or a run's whole output, cut to slots.tail_max_kib
    log: str  # where the full output was saved, for the owner


class Launcher(Protocol):
    """The warden's only ways to run anything: the three template units polkit allows, and the one restart it
    allows."""

    def start_agent(self, request_id: str, launch: dict[str, Any]) -> None: ...
    def agent_command(self, argv: list[str], cwd: str) -> tuple[int, str]: ...
    def stop_session(self, session_id: str) -> None: ...
    def message_session(self, session_id: str, text: str) -> None: ...
    def run_stack(self, slot: int, verb: list[str], session: str) -> StackResult: ...
    def run_check(self, checkout: str, args: list[str]) -> tuple[int, str]: ...
    def restart_daemon(self) -> None: ...
    def signed_in(self) -> str | None: ...
    def answered(self, session_id: str, since: float) -> str | None: ...
    def start_upgrade(self, commit: str) -> None: ...


class GitHub(Protocol):
    def dispatch_promote(self, sha: str, released: str) -> str: ...
    def full_sha(self, sha: str) -> str: ...
    def ci_run(self, sha: str) -> str | None: ...
    def run(self, run_id: str) -> dict[str, Any]: ...
    def reachable(self) -> bool: ...


class Clock:
    def now(self) -> float:
        return time.time()

    def iso(self) -> str:
        return datetime.fromtimestamp(self.now(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def age(self, iso: str) -> float:
        return self.now() - datetime.fromisoformat(iso).timestamp()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class Context:
    places: Places
    settings: Callable[[], Settings]  # the effective settings, read fresh
    caller: Caller
    clock: Clock
    launcher: Launcher
    github: GitHub
    post_event: Callable[[str, dict[str, Any]], None]  # an event for the orchestrator's inbox
    lock: threading.RLock = field(default_factory=threading.RLock)
