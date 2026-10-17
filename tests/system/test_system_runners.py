from __future__ import annotations

import html
import http.client
import json
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from system_fakes import dry_job, settings

from runtime.dashboard.server import UI
from runtime.shared import upgrades
from runtime.shared.paths import Places, stored_signin
from runtime.system import layout, root_commits
from runtime.system.install_steps import steps_machine
from runtime.system.runners import errors_run, record_exit, services_run, trust, upgrading, with_token
from runtime.system.runners import launch_file as lf
from runtime.system.runners.stack_run import size_bytes
from standard.services import provision

STATE = Path("/var/lib/software-factory")
HOME = Path("/home/software-agent")
REPO = Path("/home/software-agent/shop")
SESSION = {
    "kind": "session", "name": "impl-cart",
    "model": "claude-opus-5-5", "effort": "high", "agent": "implementer",
    "settings": f"{STATE}/public/session-settings/r1.json", "agents": "{}",
    "worktree": "impl-cart", "prompt": "-- build it",
}  # fmt: skip
SESSIONS = {"s1": {"worktree": f"{REPO}/.claude/worktrees/impl-cart"}, "s2": {"worktree": "/home/owner/elsewhere"}}
COPY = {**SESSION, "resume": "0b8f6a52-1c2d-4e5f-8a9b-0c1d2e3f4a5b"}


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
    respawn = {"kind": "command", "argv": ["claude", "respawn", "0b8f6a52"], "cwd": str(REPO)}
    respawned, _ = lf.command_argv(respawn, HOME, STATE)
    shape = lf.check_parts({"kind": "check", "checkout": str(REPO), "args": ["--only", "repo-shape"]}, HOME)

    # A start loads the skills only from the folder the warden renders, as a plugin's hooks run too.
    plugin = ["--plugin-dir", f"{STATE}/public/plugin"]
    assert argv[:3] == [str(layout.CLAUDE_BIN), *plugin]
    assert argv[-2:] == ["--", "-- build it"]  # a prompt that looks like a flag stays the prompt
    assert command[0] == "/usr/bin/git" and cwd == REPO
    assert respawned == [str(layout.CLAUDE_BIN), "respawn", "0b8f6a52"]
    assert shape == (REPO, ["--only", "repo-shape"])
    assert lf.checkout_for("s1", settings(), SESSIONS) == REPO / ".claude" / "worktrees" / "impl-cart"
    assert lf.checkout_for("", settings(), SESSIONS) == REPO
    assert "--worktree" in argv and lf.session_cwd(SESSION, settings()) == REPO
    copy = lf.session_argv(COPY, STATE)
    assert copy[3:7] == ["--bg", "--resume", COPY["resume"], "--name"] and "--worktree" not in copy
    assert lf.session_cwd(COPY, settings()) == REPO / ".claude" / "worktrees" / "impl-cart"
    assert lf.session_cwd({**COPY, "worktree": ""}, settings()) == REPO


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
        {"kind": "command", "argv": ["claude", "--resume", "0b8f6a52", "--bg", "--", "go"], "cwd": str(REPO)},
        {"kind": "command", "argv": ["claude", "respawn", "0b8f6a52", "--settings", "/tmp/x.json"], "cwd": str(REPO)},
        {"kind": "command", "argv": ["claude", "respawn", "--all"], "cwd": str(REPO)},
        {"kind": "command", "argv": ["rm", "-rf", "--", str(HOME)], "cwd": str(REPO)},
        {"kind": "command", "argv": ["rm", "-rf", "--", "/var/lib/software-factory"], "cwd": str(REPO)},
        {"kind": "command", "argv": ["kill", "-KILL", "1"], "cwd": str(REPO)},
        {**SESSION, "name": "--dangerously-skip-permissions"},
        {**SESSION, "model": "-x"},
        {**SESSION, "settings": f"{STATE}/public/session-settings/../launch/r1.json"},
        {**SESSION, "settings": "/tmp/r1.json"},
        {**SESSION, "agents": "[1, 2]"},
        {**COPY, "resume": "0b8f6a52"},
        {**COPY, "resume": "--fork-session"},
        {**COPY, "worktree": "../../.ssh"},
        {**COPY, "worktree": ".."},
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
    assert lf.run_in_terminal([sys.executable, "-c", script], tmp_path, {}, 1, 1) == status


def test_claude_gets_its_sign_in_token_on_a_pipe_and_in_no_environment(tmp_path: Path) -> None:
    (tmp_path / layout.CLAUDE_TOKEN).write_text("sk-test-token\n")
    env, fds = lf.claude_env({}, str(tmp_path))
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


@pytest.mark.parametrize(
    ("variables", "written"),
    [
        ({"EXIT_STATUS": "3", "SERVICE_RESULT": "exit-code"}, "3"),
        ({"SERVICE_RESULT": "timeout"}, "timeout"),
        ({}, "unknown"),
    ],
    ids=["an-exit-status", "a-service-result", "neither"],
)
def test_a_stopped_request_records_how_it_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variables: dict[str, str], written: str
) -> None:
    for key in ("EXIT_STATUS", "SERVICE_RESULT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in variables.items():
        monkeypatch.setenv(key, value)
    target = tmp_path / "request.exit"
    assert record_exit.main([str(target)]) == 0
    assert target.read_text() == f"{written}\n"


@pytest.mark.parametrize("before", [None, {"numStartups": 7, "projects": {"/elsewhere": {"allowedTools": []}}}],
                         ids=["no-file-yet", "a-file-claude-code-wrote"])  # fmt: skip
def test_the_agents_checkout_is_trusted_its_first_run_done_and_the_rest_of_claude_codes_file_kept(
    tmp_path: Path, before: dict[str, Any] | None
) -> None:
    config = tmp_path / ".claude.json"
    if before is not None:
        config.write_text(json.dumps(before))

    assert trust.main([str(config), "/home/software-agent/shop"]) == 0

    after = json.loads(config.read_text())
    assert after["projects"]["/home/software-agent/shop"] == {"hasTrustDialogAccepted": True}
    assert after["hasCompletedOnboarding"] is True
    assert {k: v for k, v in after.items() if k not in ("projects", "hasCompletedOnboarding")} == {
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
    assert f"run: {trusting} {job.checkout()} (as software-agent)" in job.host.actions  # type: ignore[attr-defined]  # a dry host


def test_the_install_opens_the_agents_own_files_to_a_slots_containers_and_nothing_else(tmp_path: Path) -> None:
    job = dry_job(tmp_path)
    job.settings = settings()
    steps_machine.agent_checkout(job)
    opening = next(a for a in job.host.actions if "chmod" in a)  # type: ignore[attr-defined]  # a dry host
    # As the agent, never as root: root would follow a link an agent planted to any file on the machine.
    assert opening.endswith("(as software-agent)")
    assert "! -user software-agent ) -prune" in opening and "( -type f -o -type d )" in opening


def test_the_install_gives_the_agents_checkout_the_factorys_git_identity(tmp_path: Path) -> None:
    job = dry_job(tmp_path)
    job.settings = settings()
    steps_machine.agent_checkout(job)
    configs = [a for a in job.host.actions if " config user." in a]  # type: ignore[attr-defined]  # a dry host
    assert configs == [
        f"run: git -C {job.checkout()} config user.name {root_commits.GIT_NAME} (as software-agent)",
        f"run: git -C {job.checkout()} config user.email {root_commits.GIT_EMAIL} (as software-agent)",
    ]


@pytest.mark.parametrize(("verb", "code", "status"), [("up", 0, 0), ("up", 1, 1), ("stop", 0, 0), ("stop", 1, 1)])
def test_the_factorys_own_glitchtip_starts_and_stops_on_its_own_and_says_when_it_did_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], verb: str, code: int, status: int
) -> None:
    ran: list[list[str]] = []

    def run(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        ran.append(argv)
        return subprocess.CompletedProcess(argv, code, "", "glitchtip is unhealthy")

    monkeypatch.setattr(provision, "real_run", run)

    assert errors_run.main([verb]) == status
    assert ran and all(argv[:4] == ["docker", "compose", "-p", "factory-errors"] for argv in ran)
    if status:
        assert "the factory's own GlitchTip" in capsys.readouterr().out


def test_stopping_the_shared_services_leaves_the_factorys_own_glitchtip_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[list[str]] = []
    monkeypatch.setattr(
        provision, "real_run", lambda argv, stdin=None: ran.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
    )

    assert services_run.main(["stop"]) == 0
    assert ran and not any("factory-errors" in argv for argv in ran)


def _ask(server: ThreadingHTTPServer, path: str, method: str = "GET") -> tuple[int, bytes]:
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(str(host), int(port), timeout=5)
    connection.request(method, path, body=b"{}" if method == "POST" else None)
    reply = connection.getresponse()
    return reply.status, reply.read()


@pytest.mark.parametrize(
    ("progress", "said"),
    [
        (
            (upgrades.RUNNING, "Starting the factory"),
            "The factory is upgrading: starting the factory.",
        ),
        ((upgrades.FAILED, ""), "The factory is upgrading."),
        (None, "The factory is upgrading."),
    ],
    ids=["at-a-step", "its-record-says-it-ended", "no-record-yet"],
)
def test_the_dashboards_stand_in_says_the_factory_is_upgrading_on_every_page_until_the_dashboard_is_back(
    tmp_path: Path, progress: tuple[str, str] | None, said: str
) -> None:
    places = Places.under(tmp_path)
    if progress is not None:
        upgrades.write_progress(places, "a" * 40, "b" * 40, *progress)
    server = upgrading.serve(places, 0, 7)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = _ask(server, "/tasks/t-0001")
        api = _ask(server, "/api/home")
        write = _ask(server, "/api/factory/pause", "POST")
        css = _ask(server, "/app.css")
    finally:
        server.shutdown()
        server.server_close()

    sentence = f"{said} This page comes back by itself when it is done."
    assert page[0] == 503 and html.escape(sentence) in page[1].decode()
    assert '<meta http-equiv="refresh" content="7">' in page[1].decode()
    assert (api[0], json.loads(api[1])["warden"]) == (503, {"ok": False, "since": None, "note": sentence})
    assert write[0] == 503
    assert css == (200, (UI / "app.css").read_bytes())


def test_the_daemon_is_signed_in_by_its_stored_sign_in_which_only_it_reads_and_no_token_in_its_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, loaded = tmp_path / "agent", tmp_path / "credentials"
    loaded.mkdir()
    (loaded / layout.CLAUDE_TOKEN).write_text("sk-ant-oat01-test\n")
    signin = stored_signin(home)
    signin.parent.mkdir(parents=True)
    signin.write_text(json.dumps({"mcpOAuth": {"kept": True}}))
    monkeypatch.setattr(with_token.layout, "AGENT_HOME", home)
    monkeypatch.setattr(with_token.environment, "credentials_folder", lambda: str(loaded))
    started: list[tuple[str, list[str], dict[str, str]]] = []
    monkeypatch.setattr(with_token.os, "execve", lambda path, argv, env: started.append((path, argv, env)))

    with_token.main(["/usr/local/libexec/software-factory/claude", "daemon", "run"])

    held = json.loads(signin.read_text())
    assert held["mcpOAuth"] == {"kept": True}
    assert {k: held["claudeAiOauth"][k] for k in ("accessToken", "refreshToken", "scopes")} == {
        "accessToken": "sk-ant-oat01-test",
        "refreshToken": None,
        "scopes": ["user:inference"],
    }
    assert oct(signin.stat().st_mode & 0o777) == "0o600"
    [(path, argv, env)] = started
    assert argv == [path, "daemon", "run"]
    assert lf.TOKEN_FD not in env and not any("sk-ant-oat01-test" in value for value in env.values())
