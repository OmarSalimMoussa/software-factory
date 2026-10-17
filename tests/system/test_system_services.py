"""The shared services the server set-up runs: the install checks them before it changes anything, lets the slots read
their settings, gives the factory's own errors a project there, and makes no services of its own."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest
from system_fakes import INSPECT, dry_job, settings

from runtime.shared import error_reports
from runtime.shared.protocol import Refused
from runtime.system import install, layout
from runtime.system.host import Account, DryHost
from runtime.system.install_steps import steps_machine, steps_services
from runtime.system.job import Install
from runtime.system.journal import Journal
from standard.services import glitchtip_bootstrap as glitchtip
from standard.services import provision, stack


def _host(job: Install) -> DryHost:
    assert isinstance(job.host, DryHost)
    return job.host


@pytest.mark.parametrize(
    ("stopped", "env", "refused"),
    [
        ("", True, ""),
        (provision.GLITCHTIP, True, "factory-glitchtip is not running"),
        ("", False, "services.env is missing"),
    ],
    ids=["running", "a-container-stopped", "no-settings"],
)
def test_the_install_refuses_before_changing_anything_unless_the_shared_services_run(
    tmp_path: Path, stopped: str, env: bool, refused: str
) -> None:
    job = dry_job(tmp_path)
    host = _host(job)
    said = "".join(f"/{name} {str(name != stopped).lower()}\n" for name in provision.SHARED)
    host.answers[INSPECT] = (1 if stopped else 0, said)
    if not env:
        host.paths.clear()
    steps = [step for _, step in install.STEPS]

    assert steps.index(steps_services.check) < install.FIRST_CHANGE - 1
    if refused:
        with pytest.raises(Refused, match=f"{refused}.*set the server up first"):
            steps_services.check(job)
    else:
        steps_services.check(job)


def test_the_install_makes_no_services_of_its_own_and_lets_the_slots_read_the_shared_ones(tmp_path: Path) -> None:
    job = dry_job(tmp_path)

    install.run(job)

    env, acts = job.places.services / stack.ENV_FILE, _host(job).actions
    shared = acts.index(f"run: chgrp {layout.STACK_USER} {env}")
    assert acts[shared + 1] == f"run: chmod 0640 {env}"
    assert shared > next(n for n, a in enumerate(acts) if a.startswith("run: useradd") and layout.STACK_USER in a)
    assert not any("docker compose" in a or "dockerd-rootless" in a or "software-errors" in a for a in acts)
    written = acts.index(f"write: {error_reports.dsn_path(job.places)} (640, software-warden:software-dashboard)")
    assert written < next(n for n, a in enumerate(acts) if a.startswith("run: systemctl restart software-warden"))


@pytest.mark.parametrize(
    ("groups", "joins", "leaves"),
    [((), True, False), (("docker",), False, False), (("docker", "sudo"), False, True)],
    ids=["new", "already-in", "with-another-privileged-group"],
)
def test_the_stack_user_alone_is_in_docker_and_keeps_it(
    tmp_path: Path, groups: tuple[str, ...], joins: bool, leaves: bool
) -> None:
    job = dry_job(tmp_path)
    host = _host(job)
    job.settings = settings()
    for uid, name, home in ((1002, layout.STACK_USER, layout.STACK_HOME), (1001, "software-agent", layout.AGENT_HOME)):
        host.users[name] = Account(name, uid, uid, home, (name, *groups), "")

    steps_machine.users(job)

    acts = host.actions
    assert (f"run: gpasswd -a {layout.STACK_USER} docker" in acts) is joins
    assert f"run: gpasswd --delete {layout.STACK_USER} docker" not in acts
    assert (f"run: gpasswd --delete {layout.STACK_USER} sudo" in acts) is leaves
    assert ("run: gpasswd --delete software-agent docker" in acts) is bool(groups)


def test_the_factorys_errors_get_a_project_made_as_a_slots_and_an_address_on_the_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = dry_job(tmp_path)
    host = _host(job)
    host.dry = False
    asked: list[tuple[str, list[str], dict[str, object]]] = []

    def runner(user: str, env: dict[str, str]) -> provision.Run:
        def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
            assert stdin is not None
            line = next(text for text in stdin.splitlines() if text.startswith("PARAMS = json.loads("))
            asked.append((user, argv, json.loads(ast.literal_eval(line[len("PARAMS = json.loads(") : -1]))))
            said = {"dsns": {"factory": "https://k1@box.example.ts.net:8200/7"}}
            return subprocess.CompletedProcess(argv, 0, glitchtip.RESULT + json.dumps(said) + "\n", "")

        return run

    written: dict[Path, str] = {}
    monkeypatch.setattr(host, "runner", runner)
    monkeypatch.setattr(host, "write", lambda path, text, **how: written.__setitem__(path, text))

    steps_services.errors_project(job)

    params = {"action": "workspace", "org": "notes", "projects": {"factory": "factory"}}
    assert asked == [("", provision.shell(provision.GLITCHTIP), params)]
    assert written == {error_reports.dsn_path(job.places): "http://k1@127.0.0.1:8200/7\n"}


def test_a_glitchtip_that_gives_no_project_stops_the_install_and_the_log_says_why(tmp_path: Path) -> None:
    job = dry_job(tmp_path)
    host = _host(job)
    host.dry = False
    host.journal = Journal(tmp_path / "install.log", terminal=False)
    shell = " ".join(provision.shell(provision.GLITCHTIP))
    host.answers[shell] = (3, glitchtip.REFUSED + "GlitchTip has no organization 'notes'\n")

    with pytest.raises(Refused, match="gave the factory no project for its errors"):
        steps_services.errors_project(job)

    assert "GlitchTip has no organization 'notes'" in (tmp_path / "install.log").read_text()
    assert not any(action.startswith(f"write: {error_reports.dsn_path(job.places)}") for action in host.actions)
