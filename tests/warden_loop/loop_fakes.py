"""Fakes for the warden loop's tests: the clock, GitHub, the health addresses, and git for a product checkout."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.shared import store
from runtime.warden.context import Clock, Context, StackResult
from runtime.warden.health import cpu_budget
from runtime.warden.health.probes import Answer


class FakeClock(Clock):
    def __init__(self) -> None:
        self.t = 1_791_000_000.0

    def now(self) -> float:
        return self.t


@dataclass
class FakeGitHub:
    up: bool = True

    def reachable(self) -> bool:
        return self.up


@dataclass
class FakeStack:
    """scripts/dev on slot 0, for the tick's report pull: the product lists the verbs ``verbs`` names. Prod's build
    and deploy answer as ``deploy`` says, by default at once and well."""

    logs: Path
    verbs: str = "up down health"
    calls: list[tuple[int, list[str], str]] = field(default_factory=list)
    deployed: list[tuple[str, bool]] = field(default_factory=list)  # each deploy's commit, and whether it went back
    deploy: Callable[[str], tuple[int, str]] = lambda sha: (0, "")
    slices: list[tuple[list[str], bool]] = field(default_factory=list)  # each slice change, and whether until a boot

    def run_stack(self, slot: int, verb: list[str], session: str, given: str = "") -> StackResult:
        self.calls.append((slot, verb, session))
        log = self.logs / f"stack-{len(self.calls)}.log"
        log.write_text(self.verbs if verb == ["verbs"] else "")
        return StackResult(0, "", str(log))

    def deploy_prod(self, sha: str, back: bool) -> tuple[int, str]:
        self.deployed.append((sha, back))
        return self.deploy(sha)

    def set_slice(self, properties: list[str], runtime: bool) -> None:
        self.slices.append((properties, runtime))


@dataclass
class World:
    ctx: Context
    github: FakeGitHub
    clock: FakeClock
    events: list[tuple[str, dict[str, Any]]]
    checkout: Path
    launcher: FakeStack


def kernel(tmp_path: Path) -> tuple[Path, Path, Path]:
    proc, power, cgroups = tmp_path / "proc", tmp_path / "power" / "AC", tmp_path / "cgroup"
    proc.mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text("MemAvailable:   16000000 kB\n")
    (proc / "stat").write_text("cpu  300 0 100 9600 0 0 0 0 0 0\ncpu0 300 0 100 9600 0 0 0 0 0 0\n")
    cgroup = cpu_budget.slice_dir(cgroups)
    cgroup.mkdir(parents=True, exist_ok=True)
    (cgroup / "cpu.stat").write_text("usage_usec 1000000\n")
    power.mkdir(parents=True, exist_ok=True)
    (power / "type").write_text("Mains\n")
    (power / "online").write_text("1\n")
    return proc, power.parent, cgroups


def answers(*replies: Answer | Exception) -> Callable[[str, float], Answer]:
    """A fetch that gives each reply in turn: an Answer, or an exception it raises."""
    queue = list(replies)

    def fetch(url: str, timeout: float) -> Answer:
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return fetch


def healthy(commit: str) -> Answer:
    return Answer(200, f'{{"build_commit": "{commit}"}}'.encode())


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(checkout: Path, change: Callable[[Path], None]) -> str:
    change(checkout)
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "change")
    return git(checkout, "rev-parse", "HEAD")


def passed(world: World, tree: str) -> None:
    """The warden's record of a whole check that passed on ``tree``."""
    store.write_json(world.ctx.places.check_passes, [{"tree": tree, "commit": "", "branch": "", "task": "", "at": ""}])


def write(rel: str) -> Callable[[Path], None]:
    def change(root: Path) -> None:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("changed\n")

    return change
