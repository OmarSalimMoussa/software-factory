from __future__ import annotations

import json
import threading
import time
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
    # How the warden's launcher answers a command that succeeds, then one that fails.
    warden: tuple[Ran, Ran] = (Ran(0, "true\n"), Ran(1, ""))
    check: dict[str, str] = field(default_factory=lambda: {"env": "unset", "write": "EROFS"})  # outcome by kind

    def as_user(self, user: str, argv: list[str], *, view: bool = False) -> Ran:
        return Ran(1, "")

    def guard(self, tool: str, tool_input: dict[str, Any]) -> Ran:
        return self.guard_answer

    def sandboxed(self, *commands: str, extra: dict[str, Any] | None = None, properties: tuple[str, ...] = ()) -> Ran:
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

    def warden_command(self, argv: list[str]) -> Ran:
        return self.warden[0] if "--is-inside-work-tree" in argv else self.warden[1]

    def warden_check(self, probe: list[str]) -> tuple[Path, Ran]:
        """The check's report: its deps printed the probe's line for each target, each with ``self.check``."""
        lines = [f"{target} -> {self.check.get(target.partition(':')[0], 'ENOENT')}" for target in probe[4:]]
        return Path("/home/software-agent/drill-check"), Ran(
            1, "factory check: deps failed: probe:\n" + "\n".join(lines)
        )


@pytest.mark.parametrize(
    ("warden", "ok"),
    [
        ((Ran(0, "true\n"), Ran(1, "")), True),
        # The exit recorder could not write its file: the warden learns nothing, and no session can start.
        ((Ran(-1, "software-agent-run@cmd-1.service left no result (FileNotFoundError)"), Ran(-1, "")), False),
        # The exit came back but the output was emptied: the warden cannot read what a command printed.
        ((Ran(0, ""), Ran(1, "")), False),
        ((Ran(0, "true\n"), Ran(0, "")), False),  # a failing command must come back failed
    ],
    ids=["reads-both", "no-result", "output-lost", "failure-hidden"],
)
def test_the_warden_command_drill_needs_the_output_and_the_exit_of_a_real_unit_run(
    warden: tuple[Ran, Ran], ok: bool
) -> None:
    assert drills.warden_runs_commands(FakeRig(warden=warden)).ok is ok


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


def _runs(*runs: tuple[str, str, bool]) -> str:
    """A session's stream in which it ran each command, with what it printed and whether it failed."""
    lines: list[dict[str, Any]] = []
    for n, (command, result, error) in enumerate(runs):
        use = {"type": "tool_use", "id": f"t{n}", "name": "Bash", "input": {"command": command}}
        done = {"type": "tool_result", "tool_use_id": f"t{n}", "content": result, "is_error": error}
        lines += [
            {"type": "assistant", "message": {"content": [use]}},
            {"type": "user", "message": {"content": [done]}},
        ]
    return "".join(json.dumps(line) + "\n" for line in lines)


COPY = "/opt/software-factory-toolchain/python/cpython-3.14.4-linux-x86_64-gnu/bin/python3.14"


@pytest.mark.parametrize(
    ("find", "ok"),
    [
        ((COPY, False), True),
        (("error: No interpreter found for Python 3.14 in managed installations or search path", True), False),
        # Found where no unit's uv should look: the toolchain's copy was not what answered.
        (("/home/software-agent/.local/share/uv/python/cpython-3.14.4-linux-x86_64-gnu/bin/python3.14", False), False),
        (None, False),  # the model never ran it: inconclusive
    ],
)  # fmt: skip
def test_the_toolchain_drill_needs_every_program_and_the_products_python_from_the_copy(
    tmp_path: Path, find: tuple[str, bool] | None, ok: bool
) -> None:
    (tmp_path / ".python-version").write_text("3.14\n")
    asked: list[str] = []

    class Session(FakeRig):
        def sandboxed(
            self, *commands: str, extra: dict[str, Any] | None = None, properties: tuple[str, ...] = ()
        ) -> Ran:
            asked.extend(commands)
            ran = [("uv --version", "uv 0.11.6", False), ("node --version", "v22.1.0", False)]
            return Ran(0, _runs(*ran, *([("uv python find 3.14", *find)] if find else [])))

    result = drills.toolchain_answers(Session(worktree_a=tmp_path))

    assert asked == ["uv --version", "node --version", "uv python find 3.14"]
    assert result.ok is ok, result.evidence


def test_a_drill_that_breaks_fails_by_name_and_the_rest_still_run() -> None:
    class Broken(FakeRig):
        def as_user(self, user: str, argv: list[str], *, view: bool = False) -> Ran:
            raise OSError("systemd-run is gone")

    results = drills.run_all(Broken(), 4)

    assert len(results) == len(drills.DRILLS)
    assert next(r for r in results if r.name == "docker_denied").evidence.startswith("the drill broke: OSError")


@dataclass
class Overlapping(FakeRig):
    """Sessions that take a while, counting how many run at once."""

    running: int = 0
    most: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def sandboxed(self, *commands: str, extra: dict[str, Any] | None = None, properties: tuple[str, ...] = ()) -> Ran:
        with self.lock:
            self.running += 1
            self.most = max(self.most, self.running)
        time.sleep(0.05)
        with self.lock:
            self.running -= 1
        return super().sandboxed(*commands, extra=extra, properties=properties)


def test_session_drills_run_side_by_side_and_every_drill_reports_once_in_order() -> None:
    rig = Overlapping()

    results = drills.run_all(rig, 3)

    named = {drill: drill(FakeRig()).name for drill in drills.DRILLS}
    assert [r.name for r in results] == [named[drill] for drill in drills.DRILLS]
    assert rig.most == 3


def test_the_install_refuses_to_finish_while_a_drill_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from runtime.system import drill_rig

    @contextmanager
    def opened(ctx: object) -> Iterator[FakeRig]:
        yield FakeRig()

    monkeypatch.setattr(drill_rig, "opened", opened)
    monkeypatch.setattr(drills, "run_all", lambda rig, at_once: [Result("a", True, "held"),
                                                                  Result("b", False, "breached")])  # fmt: skip
    job = dry_job(tmp_path)
    job.host.dry, job.settings = False, settings()

    with pytest.raises(Refused, match="1 of the safety checks failed"):
        install.run_drills(job)
    assert job.drilled == [
        {"name": "a", "ok": True, "evidence": "held"},
        {"name": "b", "ok": False, "evidence": "breached"},
    ]


@dataclass
class OneAnswer(FakeRig):
    """A session whose one Bash call answers ``result``."""

    result: str = ""
    error: bool = True

    def sandboxed(self, *commands: str, extra: dict[str, Any] | None = None, properties: tuple[str, ...] = ()) -> Ran:
        use = {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": commands[-1]}}]
            },
        }
        done = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": self.result, "is_error": self.error}
                ]
            },
        }
        return Ran(0, json.dumps(use) + "\n" + json.dumps(done) + "\n")  # fmt: skip


@pytest.mark.parametrize(
    ("result", "error", "ok"),
    [
        (drills.SANDBOX_MARK, False, True),
        ("Exit code 1\nbwrap: setting up uid map: Permission denied", True, False),
    ],
    ids=["runs", "sandbox-broken"],
)
def test_the_control_drill_needs_a_plain_command_to_run_in_the_sandbox(result: str, error: bool, ok: bool) -> None:
    found = drills.sandbox_runs(OneAnswer(result=result, error=error))

    assert found.ok is ok
    assert ok or "bwrap: setting up uid map" in found.evidence  # the whole error, not its first line


@pytest.mark.parametrize(
    ("result", "ok"),
    [
        ("unix:/run/docker.sock -> EPERM\nunix:/run/software-factory/warden.sock -> EPERM", True),
        ("Refused by the factory guard (factory-alone): run factory on its own", True),
        ("Exit code 1", False),  # a sandbox that runs nothing proves nothing
    ],
    ids=["refused-each", "guard-refused", "bare-failure"],
)
def test_a_refusal_drill_passes_only_on_an_answer_that_names_the_refusal(result: str, ok: bool) -> None:
    assert drills.sockets_refused(OneAnswer(result=result)).ok is ok


@dataclass
class Counted(OneAnswer):
    """Runs nothing the first ``silent`` times it is asked, then answers as OneAnswer does."""

    silent: int = 1
    asks: list[tuple[str, ...]] = field(default_factory=list)

    def sandboxed(self, *commands: str, extra: dict[str, Any] | None = None, properties: tuple[str, ...] = ()) -> Ran:
        self.asks.append(properties)
        if len(self.asks) <= self.silent:
            return Ran(124, "the drill ran past 180 s")
        return super().sandboxed(*commands, extra=extra, properties=properties)


@pytest.mark.parametrize(("silent", "ok", "asks"), [(1, True, 2), (2, False, 2)], ids=["second-try", "both-silent"])
def test_a_session_that_ran_nothing_is_asked_once_more(silent: int, ok: bool, asks: int) -> None:
    rig = Counted(result=drills.SANDBOX_MARK, error=False, silent=silent)

    assert drills.sandbox_runs(rig).ok is ok
    assert len(rig.asks) == asks


def test_the_bubblewrap_drill_hides_bubblewrap_and_fails_when_the_command_still_runs() -> None:
    rig = Counted(result="env:PATH -> set", error=False, silent=0)

    result = drills.bubblewrap_missing(rig)

    assert rig.asks == [(f"InaccessiblePaths={drills.layout.BWRAP}",)]
    assert not result.ok and "without the sandbox" in result.evidence


@pytest.mark.parametrize(
    ("check", "ok"),
    [
        ({"env": "unset", "write": "EROFS"}, True),
        ({"env": "unset", "write": "written"}, False),
        ({"env": "unset", "write": "EROFS", "file": "opened"}, False),
        ({"env": "set", "write": "EROFS"}, False),
    ],
    ids=["walled-in", "git-folder-written", "sign-in-read", "token-in-environment"],
)
def test_the_check_drill_fails_on_any_way_out_of_the_checkout(check: dict[str, str], ok: bool) -> None:
    assert drills.check_walled_in(FakeRig(check=check)).ok is ok


@pytest.mark.parametrize(
    ("plain", "container", "ok"),
    [
        ("ECONNREFUSED", "refused", True),
        ("connected", "refused", False),
        ("ECONNREFUSED", "connected", False),
        ("ECONNREFUSED", "", False),  # the container never ran: nothing is proved
    ],
    ids=["walled-off", "stack-user-through", "container-through", "no-container"],
)
def test_the_stack_drill_needs_both_the_user_and_a_container_walled_off(plain: str, container: str, ok: bool) -> None:
    class Stack(FakeRig):
        def as_user(self, user: str, argv: list[str], *, view: bool = False) -> Ran:
            if argv[:2] == ["docker", "inspect"]:
                return Ran(0, "docker.io/postgres:17.10\n")
            if argv[:2] == ["docker", "run"]:
                return Ran(0, f"tcp:{drills.TAILNET_PROBE}:53 -> {container}\n" if container else "")
            return Ran(0, "".join(f"{t} -> {plain}\n" for t in argv[4:]))

    assert drills.stack_off_tailnet(Stack()).ok is ok
