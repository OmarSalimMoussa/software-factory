from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from lifecycle_fakes import World

from runtime.shared import store
from runtime.shared.protocol import Refused
from runtime.warden import doing, launch, lifecycle, session_settings
from runtime.warden.launch import LaunchError

FIXTURE = Path(__file__).resolve().parents[2] / "testdata" / "fixture-product"


@pytest.mark.parametrize("role", ["orchestrator", "implementer"])
def test_each_session_starts_sandboxed_in_its_worktree(world: World, tmp_path: Path, role: str) -> None:
    repo = Path(world.settings.get_str("repo.path"))
    own = repo if role == "orchestrator" else repo / ".claude" / "worktrees" / "impl-t-0001"
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
    assert str(repo / ".claude" / "worktrees") in sandbox["filesystem"]["denyRead"]
    assert str(tmp_path / "agent" / ".claude" / ".credentials.json") in sandbox["filesystem"]["denyRead"]
    assert body["env"] == {"CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1"}
    assert ("statusLine" in body) == (role == "orchestrator")


def test_a_session_gets_its_role_agents_with_their_browser_servers(world: World) -> None:
    world.approve()
    lifecycle.start(world.ctx(), {"role": "implementer", "task": "t-0001"}, None)
    request = world.launcher.started[0][1]
    agents = json.loads(request["agents"])
    assert set(agents) == {"implementer", "qa-backend", "qa-security", "qa-ui-function", "qa-ux", "qa-visual"}
    assert (agents["implementer"]["model"], agents["implementer"]["effort"]) == (request["model"], request["effort"])
    assert agents["qa-ui-function"]["mcpServers"][0]["qa-browser"]["args"] == ["qa-browser", "qa-ui-function"]
    assert launch.session_argv(request)[-2:] == ["--", "Task: t-0001"]


def test_a_stack_run_hands_back_a_redacted_tail(world: World) -> None:
    shutil.copy(FIXTURE / "factory.toml", Path(world.settings.get_str("repo.path")) / "factory.toml")
    # NOTES_ORIGIN is one of factory.toml's [env.set] keys: the slot's own address, which stays readable.
    env = "DB_PASSWORD=s3cret-pw\nPORT=8102\nTOKEN='tok-123456'\nNOTES_ORIGIN=http://127.0.0.1:8102\n"
    store.write_text(world.places.slot_env_dir(2) / "notes.env", env)
    # What the product's scripts/dev writes beside the env file is never read as one.
    store.write_text(world.places.slot_env_dir(2) / "seeds" / "users.json", '{"PORT=8102": 1}')
    (world.places.slot_env_dir(2) / "dev.tmp").write_bytes(b"\xff\xfe not text")
    out = "\n".join(
        ["starting", "connect with s3cret-pw on 8102", "token tok-123456", *[f"line {n}" for n in range(50)],
         "http://127.0.0.1:8102/health"]
    )  # fmt: skip

    def systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
        request = argv[-1].split("@", 1)[1].removesuffix(".service")
        store.write_text(world.places.state / "runs" / f"{request}.log", out)
        store.write_text(world.places.state / "runs" / f"{request}.exit", "3\n")
        return subprocess.CompletedProcess(argv, 1, "", "")

    result = launch.Units(world.places, lambda: world.settings, systemctl).run_stack(2, ["up"], "5e550001")
    tail = world.settings.get_int("slots.log_tail_lines")
    assert result.exit == 3 and len(result.tail.splitlines()) == tail
    saved = Path(result.log).read_text()
    assert "s3cret-pw" not in saved and "tok-123456" not in saved and "8102" in saved
    assert result.tail.endswith("http://127.0.0.1:8102/health")
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
