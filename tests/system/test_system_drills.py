from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from system_fakes import dry_job, settings

from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.system import drills, install
from runtime.system.drills import Ran, Result


def _stream(command: str, result: str) -> str:
    use = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": command}}]},
    }
    done = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": result, "is_error": True}]},
    }
    return json.dumps(use) + "\nnot json\n" + json.dumps(done) + "\n"  # fmt: skip


@dataclass
class FakeRig:
    places: Places = field(default_factory=lambda: Places.system())
    settings: Any = field(default_factory=settings)
    worktree_a: Path = Path("/w/a")
    worktree_b: Path = Path("/w/b")
    branch_b: str = "b"
    finished_agent: str = "intake1"
    guard_answer: Ran = Ran(0, "")
    ran: str = ""  # the command the fake model runs
    written: bool = False
    clock: float = 0.0
    claude_gone_at: float | None = None

    def as_user(self, user: str, argv: list[str], *, view: bool = False) -> Ran:
        return Ran(1, "")

    def guard(self, tool: str, tool_input: dict[str, Any]) -> Ran:
        return self.guard_answer

    def sandboxed(self, *commands: str, extra: dict[str, Any] | None = None) -> Ran:
        return Ran(0, _stream(self.ran or commands[-1], "touch: cannot touch: Read-only file system"))

    def exists(self, path: Path) -> bool:
        return self.written

    def connect(self, host: str, port: int) -> str:
        return "connected"

    def background(self, user: str, argv: list[str]) -> str:
        return "drill.service"

    def claude_pids(self, unit: str) -> list[int]:
        return [] if self.claude_gone_at is not None and self.clock >= self.claude_gone_at else [42]

    def stop(self, unit: str) -> None:
        pass

    def wait(self, seconds: float) -> None:
        self.clock += seconds

    def now(self) -> float:
        return self.clock


@pytest.mark.parametrize(
    ("answer", "ok"),
    [
        (Ran(2, "Refused by the factory guard (own-worktree): edit only inside your own worktree"), True),
        # Refused, but by another rule: that proves nothing about own-worktree.
        (Ran(2, "Refused by the factory guard (unpermitted): this session holds no permit"), False),
        (Ran(0, ""), False),
    ],
)
def test_a_guard_drill_passes_only_on_its_own_rule(answer: Ran, ok: bool) -> None:
    assert drills.edit_other_worktree(FakeRig(guard_answer=answer)).ok is ok


@pytest.mark.parametrize(
    ("ran", "written", "ok"),
    [
        ("", False, True),
        ("", True, False),
        ("echo something else", False, False),  # the model ran another command: inconclusive
    ],
)
def test_a_sandbox_drill_needs_the_exact_command_and_the_wall_holding(ran: str, written: bool, ok: bool) -> None:
    result = drills.shell_write_other_worktree(FakeRig(ran=ran, written=written))

    assert result.ok is ok


@pytest.mark.parametrize(("gone_at", "ok"), [(10.0, True), (None, False)])
def test_the_backstop_drill_allows_one_tick_after_the_next(gone_at: float | None, ok: bool) -> None:
    assert drills.backstop_stops_claude(FakeRig(claude_gone_at=gone_at)).ok is ok


def test_a_drill_that_breaks_fails_by_name_and_the_rest_still_run() -> None:
    class Broken(FakeRig):
        def as_user(self, user: str, argv: list[str], *, view: bool = False) -> Ran:
            raise OSError("systemd-run is gone")

    results = drills.run_all(Broken())

    assert len(results) == len(drills.DRILLS)
    assert next(r for r in results if r.name == "docker_denied").evidence.startswith("the drill broke: OSError")


def test_the_install_refuses_to_finish_while_a_drill_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runtime.system import drill_rig

    @contextmanager
    def opened(ctx: object) -> Iterator[FakeRig]:
        yield FakeRig()

    monkeypatch.setattr(drill_rig, "opened", opened)
    monkeypatch.setattr(drills, "run_all", lambda rig: [Result("a", True, "held"), Result("b", False, "breached")])
    job = dry_job(tmp_path)
    job.host.dry = False

    with pytest.raises(Refused, match="1 drill"):
        install.run_drills(job)
    assert job.drilled == [
        {"name": "a", "ok": True, "evidence": "held"},
        {"name": "b", "ok": False, "evidence": "breached"},
    ]
