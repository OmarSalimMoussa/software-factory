from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from system_fakes import dry_job, settings

from runtime.system import layout, steps_machine
from runtime.system.runners import launch_file as lf
from runtime.system.runners import trust
from runtime.system.runners.stack_run import size_bytes

STATE = Path("/var/lib/software-factory")
HOME = Path("/home/software-agent")
REPO = Path("/home/software-agent/shop")
SESSION = {
    "kind": "session", "name": "impl-cart",
    "model": "claude-opus-5-5", "effort": "high", "agent": "implementer",
    "settings": f"{STATE}/public/session-settings/r1.json", "agents": "{}",
    "worktree": "impl-cart", "prompt": "-- build it",
}  # fmt: skip
RESUME = [
    "claude", "--resume", "0b8f6a52-3c1e-4f7a-9d2b-5e6c7d8e9f01", "--bg", "--remote-control", "impl-cart",
    "--model", "claude-opus-5-5",
    "--effort", "high", "--agent", "implementer", "--settings", f"{STATE}/public/session-settings/s1.json",
    "--agents", '{"qa-ux": {"model": "claude-sonnet-5-5"}}', "--", "-- carry on",
]  # fmt: skip
SESSIONS = {"s1": {"worktree": f"{REPO}/.claude/worktrees/impl-cart"}, "s2": {"worktree": "/home/owner/elsewhere"}}


def _parse(raw: dict[str, Any]) -> object:
    kind = raw["kind"]
    if kind == lf.SESSION:
        return lf.session_argv(raw, STATE)
    if kind == lf.COMMAND:
        return lf.command_argv(raw, HOME, STATE)
    if kind == lf.CHECK:
        return lf.check_parts(raw, HOME)
    slot, _, session = lf.stack_parts(raw, settings())
    return lf.checkout_for(session, settings(), SESSIONS)


def test_each_kind_runs_only_the_installed_programs() -> None:
    argv = lf.session_argv(SESSION, STATE)
    command, cwd = lf.command_argv(
        {"kind": "command", "argv": ["git", "worktree", "list"], "cwd": str(REPO)}, HOME, STATE
    )
    resumed, _ = lf.command_argv({"kind": "command", "argv": RESUME, "cwd": str(REPO)}, HOME, STATE)
    shape = lf.check_parts({"kind": "check", "checkout": str(REPO), "args": ["--only", "repo-shape"]}, HOME)

    assert argv[0] == str(layout.CLAUDE_BIN)
    assert argv[-2:] == ["--", "-- build it"]  # a prompt that looks like a flag stays the prompt
    assert command[0] == "/usr/bin/git" and cwd == REPO
    assert resumed == [str(layout.CLAUDE_BIN), *RESUME[1:]]
    assert shape == (REPO, ["--only", "repo-shape"])
    assert lf.checkout_for("s1", settings(), SESSIONS) == REPO / ".claude" / "worktrees" / "impl-cart"
    assert lf.checkout_for("", settings(), SESSIONS) == REPO


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "command", "argv": ["sh", "-c", "id"], "cwd": str(HOME)},
        {"kind": "command", "argv": ["/usr/bin/git", "status"], "cwd": str(HOME)},
        {"kind": "command", "argv": ["git", "status"], "cwd": "/etc"},
        {"kind": "command", "argv": ["git", "status"], "cwd": f"{HOME}/../software-warden"},
        {"kind": "command", "argv": ["git", "status"], "cwd": "shop"},
        {"kind": "command", "argv": ["/usr/local/bin/factory", "check", "--all"], "cwd": str(REPO)},
        {"kind": "command", "argv": ["claude", "--bg", "--settings", "/tmp/open.json", "--", "go"], "cwd": str(REPO)},
        {"kind": "command", "argv": [*RESUME[:13], "/tmp/open.json", *RESUME[14:]], "cwd": str(REPO)},
        {"kind": "command", "argv": [*RESUME[:15], "not json", *RESUME[16:]], "cwd": str(REPO)},
        {"kind": "command", "argv": RESUME[:-2], "cwd": str(REPO)},
        {"kind": "command", "argv": ["rm", "-rf", "--", str(HOME)], "cwd": str(REPO)},
        {"kind": "command", "argv": ["rm", "-rf", "--", "/var/lib/software-factory"], "cwd": str(REPO)},
        {"kind": "command", "argv": ["kill", "-KILL", "1"], "cwd": str(REPO)},
        {**SESSION, "name": "--dangerously-skip-permissions"},
        {**SESSION, "model": "-x"},
        {**SESSION, "settings": f"{STATE}/public/session-settings/../launch/r1.json"},
        {**SESSION, "settings": "/tmp/r1.json"},
        {**SESSION, "agents": "[1, 2]"},
        {"kind": "stack", "slot": 9, "verb": ["up"], "session": "s1"},
        {"kind": "stack", "slot": True, "verb": ["up"], "session": "s1"},
        {"kind": "stack", "slot": 1, "verb": ["rm", "-rf"], "session": "s1"},
        {"kind": "stack", "slot": 1, "verb": [], "session": "s1"},
        {"kind": "stack", "slot": 1, "verb": ["up"], "session": "s2"},
        {"kind": "check", "checkout": str(HOME), "args": []},
        {"kind": "check", "checkout": "/etc", "args": []},
        {"kind": "check", "checkout": f"{HOME}/../software-warden", "args": []},
        {"kind": "check", "checkout": str(REPO), "args": ["--list"]},
        {"kind": "check", "checkout": str(REPO), "args": ["--only"]},
        {"kind": "check", "checkout": str(REPO), "args": ["--only", "--all"]},
        {"kind": "check", "checkout": str(REPO), "args": ["--since", "HEAD; rm -rf ~"]},
    ],
)
def test_a_request_outside_its_bounds_is_refused_before_anything_runs(raw: dict[str, Any]) -> None:
    with pytest.raises(lf.BadRequest):
        _parse(raw)


@pytest.mark.parametrize(("text", "size"), [("0B", 0), ("512kB", 512_000), ("1.25GB", 1_250_000_000), ("2TB", 2e12)])
def test_docker_sizes_are_decimal(text: str, size: float) -> None:
    assert size_bytes(text) == size


@pytest.mark.parametrize(
    ("raw", "ok"),
    [({"session": "0b8f6a52-3c1e-4f7a-9d2b-5e6c7d8e9f01", "text": "Skip dark mode."}, True),
     ({"session": "0b8f6a52-3c1e-4f7a-9d2b-5e6c7d8e9f01", "text": "--dangerously-skip-permissions"}, False),
     ({"session": "--resume", "text": "hi"}, False)],
    ids=["message", "flag-as-text", "flag-as-session"],
)  # fmt: skip
def test_a_message_request_names_a_session_and_text_that_cannot_be_read_as_flags(raw: dict[str, Any], ok: bool) -> None:
    if ok:
        assert lf.message_argv(raw)[-1] == "Skip dark mode."
    else:
        with pytest.raises(lf.BadRequest):
            lf.message_argv(raw)


@pytest.mark.parametrize(("script", "status"), [("import time; time.sleep(30)", 0), ("raise SystemExit(3)", 3)],
                         ids=["held-then-let-go", "failed-on-its-own"])  # fmt: skip
def test_a_message_is_held_out_in_a_terminal_and_a_failure_of_its_own_is_reported(
    tmp_path: Path, script: str, status: int
) -> None:
    assert lf.run_in_terminal([sys.executable, "-c", script], tmp_path, {}, 1) == status


def test_claude_gets_its_sign_in_token_on_a_pipe_and_in_no_environment(tmp_path: Path) -> None:
    (tmp_path / layout.CLAUDE_TOKEN).write_text("sk-test-token\n")
    env, fds = lf.claude_env({"CREDENTIALS_DIRECTORY": str(tmp_path)})
    # What Claude Code does with the descriptor its variable names: read the token to the pipe's end.
    reads = (f"import os; token = os.read(int(os.environ['{lf.TOKEN_FD}']), 4096).decode().strip(); "
             "raise SystemExit(0 if token == 'sk-test-token' else 3)")  # fmt: skip
    try:
        assert not any("sk-test-token" in value for value in env.values())
        assert "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB" not in env
        assert lf.run([sys.executable, "-c", reads], tmp_path, env, 30, fds) == 0
    finally:
        for fd in fds:
            os.close(fd)


@pytest.mark.parametrize("before", [None, {"numStartups": 7, "projects": {"/elsewhere": {"allowedTools": []}}}],
                         ids=["no-file-yet", "a-file-claude-code-wrote"])  # fmt: skip
def test_the_agents_checkout_is_trusted_and_the_rest_of_claude_codes_file_kept(
    tmp_path: Path, before: dict[str, Any] | None
) -> None:
    config = tmp_path / ".claude.json"
    if before is not None:
        config.write_text(json.dumps(before))

    assert trust.main([str(config), "/home/software-agent/shop"]) == 0

    after = json.loads(config.read_text())
    assert after["projects"]["/home/software-agent/shop"] == {"hasTrustDialogAccepted": True}
    assert {k: v for k, v in after.items() if k != "projects"} == {
        k: v for k, v in (before or {}).items() if k != "projects"
    }
    assert all(after["projects"][p] == v for p, v in (before or {}).get("projects", {}).items())
    assert oct(config.stat().st_mode & 0o777) == "0o600"


def test_the_install_has_the_agent_user_trust_its_checkout(tmp_path: Path) -> None:
    job = dry_job(tmp_path)
    job.settings = settings()
    steps_machine.agent_checkout(job)
    trusting = (
        f"{layout.PYTHON} -I {job.places.install}/runtime/system/runners/trust.py {layout.AGENT_HOME}/.claude.json"
    )
    assert f"run: {trusting} {job.checkout()} (as software-agent)" in job.host.actions  # type: ignore[attr-defined]
