from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from lifecycle_fakes import World

from runtime.shared import store
from runtime.shared.protocol import Refused
from runtime.warden.sessions import doing, launch, lifecycle, session_settings
from runtime.warden.sessions.launch import LaunchError

FIXTURE = Path(__file__).resolve().parents[2] / "testdata" / "fixture-product"


@pytest.mark.parametrize("role", ["orchestrator", "implementer"])
def test_each_session_starts_sandboxed_in_its_worktree(world: World, tmp_path: Path, role: str) -> None:
    repo = Path(world.settings.get_str("repo.path"))
    folder = repo / ".claude" / "worktrees"
    own = repo if role == "orchestrator" else folder / "impl-t-0001"
    other = folder / "impl-t-0002"
    for worktree in (own, other):
        worktree.mkdir(parents=True, exist_ok=True)
    body = session_settings.render(world.settings, world.places, role, own, tmp_path / "agent")
    sandbox = body["sandbox"]
    assert (sandbox["enabled"], sandbox["failIfUnavailable"], sandbox["allowUnsandboxedCommands"]) == (
        True,
        True,
        False,
    )
    assert sandbox["excludedCommands"] == ["factory", "factory *"]
    # Its own worktree, and its tools' caches in the agent's home (sandbox.writable_caches), nothing else.
    caches = [str(tmp_path / "agent" / ".cache" / "uv"), str(tmp_path / "agent" / ".npm")]
    assert sandbox["filesystem"]["allowWrite"] == [str(own), *caches]
    if role == "orchestrator":
        assert str(folder) in sandbox["filesystem"]["denyRead"]
        assert sandbox["filesystem"]["denyWrite"] == [str(folder)]
    else:
        # Another session's worktree is denied by name, never the folder holding the session's own: a worktree read
        # back inside a denied folder is mounted read-only, and no command can start in it.
        assert str(other) in sandbox["filesystem"]["denyRead"]
        assert not any(own.is_relative_to(Path(p)) for p in sandbox["filesystem"]["denyRead"])
        assert "allowRead" not in sandbox["filesystem"]
    assert str(tmp_path / "agent" / ".claude" / ".credentials.json") in sandbox["filesystem"]["denyRead"]
    assert body["worktree"] == {"bgIsolation": "none"}
    # The scrub runs every command sandboxed, the excluded `factory` too, which then cannot reach the warden.
    assert "env" not in body
    assert ("statusLine" in body) == (role == "orchestrator")


def test_a_session_gets_its_role_agents_with_their_browser_servers(world: World) -> None:
    world.approve()
    lifecycle.start(world.ctx(), {"role": "implementer", "task": "t-0001"}, None)
    request = world.launcher.started[0][1]
    agents = json.loads(request["agents"])
    assert set(agents) == {"implementer", "qa-backend", "qa-security", "qa-ui-function", "qa-ux", "qa-visual"}
    assert (agents["implementer"]["model"], agents["implementer"]["effort"]) == (request["model"], request["effort"])
    assert agents["qa-ui-function"]["mcpServers"][0]["qa-browser"]["args"] == ["qa-browser", "qa-ui-function"]
    assert launch.session_argv(request)[-2:] == [
        "--",
        "Task: t-0001, Task t-0001\n\n`factory queue task show t-0001` gives its log, its requests and its spec."
        "\n\nSlot 1 is yours: bring it up with factory slot up.",
    ]


def test_a_stack_run_keeps_what_the_runner_printed_and_hands_back_its_tail(world: World) -> None:
    out = "\n".join(["starting", *[f"line {n}" for n in range(50)], "http://127.0.0.1:8102/health"])

    def systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
        request = argv[-1].split("@", 1)[1].removesuffix(".service")
        store.write_text(world.places.state / "runs" / f"{request}.log", out)
        store.write_text(world.places.state / "runs" / f"{request}.exit", "3\n")
        return subprocess.CompletedProcess(argv, 1, "", "")

    result = launch.Units(world.places, lambda: world.settings, systemctl).run_stack(2, ["up"], "5e550001")
    tail = world.settings.get_int("slots.log_tail_lines")
    assert result.exit == 3 and len(result.tail.splitlines()) == tail
    assert Path(result.log).read_text() == out and result.tail.endswith("http://127.0.0.1:8102/health")
    assert not list((world.places.state / "runs").iterdir())


def test_a_resume_runs_under_the_sessions_sandbox_settings_again(world: World) -> None:
    s = world.seat("1234abcd-0000", worktree=world.settings.get_str("repo.path"))
    launch.resume(world.ctx(), s, "carry on")
    argv = world.launcher.commands[-1]
    settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
    assert settings["sandbox"]["enabled"] is True and "implementer" in json.loads(argv[argv.index("--agents") + 1])


def test_a_resume_that_starts_a_copy_removes_it_and_fails(world: World) -> None:
    world.launcher.copy = True
    with pytest.raises(LaunchError):
        launch.resume(world.ctx(), world.seat("1234abcd-0000"), "carry on")
    assert ["claude", "rm", "0badc0de"] in world.launcher.commands


@pytest.mark.parametrize("text", ["x" * 61, "<b>bold</b>", "a & b", "\x1b[31mred"])
def test_a_session_own_doing_line_stays_short_and_plain(text: str) -> None:
    with pytest.raises(Refused):
        doing.clean(text)


@pytest.mark.parametrize("exit_code", [0, 1])
def test_a_session_start_is_waited_on_and_leaves_no_request_or_output_behind(world: World, exit_code: int) -> None:
    def systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "--wait"
        request = argv[-1].split("@", 1)[1].removesuffix(".service")
        store.write_text(world.places.state / "runs" / f"{request}.log", "backgrounded · 0f00ba12\n")
        store.write_text(world.places.state / "runs" / f"{request}.exit", f"{exit_code}\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    units = launch.Units(world.places, lambda: world.settings, systemctl)
    if exit_code:
        with pytest.raises(launch.LaunchError, match="did not start"):
            units.start_agent("pending-1", {"name": "impl-t-0001"})
    else:
        units.start_agent("pending-1", {"name": "impl-t-0001"})

    assert not list((world.places.state / "public" / "launch").iterdir())
    assert not list((world.places.state / "runs").iterdir())
