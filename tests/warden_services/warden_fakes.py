from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from settings_text import set_line

from runtime.shared import totp
from runtime.shared.paths import Places
from runtime.warden.context import Caller, Clock, StackResult

ROOT = Path(__file__).resolve().parents[2]
T0 = 1_791_546_000.0  # 2026-10-09 11:40:00 UTC, on a 30-second step boundary
REPO = {
    "repo.path": "/srv/checkout",
    "repo.github": "depot/product",
}
OWNER_CALLER = Caller(kind="owner", role="owner")


class FakeClock(Clock):
    def __init__(self, now: float = T0) -> None:
        self.t = now

    def now(self) -> float:
        return self.t


class FakeLauncher:
    """Records every call; ``commands`` maps an argv's first words to the (exit, output) it answers."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.commands: dict[tuple[str, ...], tuple[int, str]] = {}
        self.stack: dict[str, StackResult] = {}
        self.upgrades: list[str] = []

    def start_agent(self, request_id: str, launch: dict[str, Any]) -> None:
        self.calls.append(("start", request_id))

    def agent_command(self, argv: list[str], cwd: str) -> tuple[int, str]:
        self.calls.append(("command", (argv, cwd)))
        for size in range(len(argv), 0, -1):
            if tuple(argv[:size]) in self.commands:
                return self.commands[tuple(argv[:size])]
        return 0, ""

    def stop_session(self, session_id: str) -> None:
        self.calls.append(("stop", session_id))

    def start_upgrade(self, commit: str) -> None:
        self.upgrades.append(commit)

    def run_stack(self, slot: int, verb: list[str], session: str, given: str = "") -> StackResult:
        self.calls.append(("stack", (slot, verb, session)))
        return self.stack.get(verb[0], StackResult(exit=0, tail="", log="stack.log"))


class FakeGitHub:
    def reachable(self) -> bool:
        return True


def defaults_text(**extra: Any) -> str:
    """The repo's own settings.toml, with the values the installer fills in."""
    text = (ROOT / "settings.toml").read_text()
    for key, value in {**REPO, **extra}.items():
        text = set_line(text, key, value)
    return text


def code_now(places: Places, clock: Clock) -> str:
    return totp.code_at(places.totp_secret.read_text().strip(), int(clock.now() // totp.STEP_S))


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()
