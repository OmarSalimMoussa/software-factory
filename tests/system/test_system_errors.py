"""The factory's own GlitchTip: its own account and settings, its set-up, and the product's GlitchTip it is never
mistaken for."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from system_fakes import dry_job

from runtime.shared import error_reports
from runtime.shared.protocol import Refused
from runtime.system import install, layout
from runtime.system.errors import instance
from runtime.system.host import Account, DryHost
from runtime.system.install_steps import steps_services
from runtime.system.job import Install
from runtime.warden.health import telemetry
from standard import yaml_subset
from standard.services import glitchtip_bootstrap as glitchtip
from standard.services import provision, stack


def test_its_secrets_are_made_once_and_kept_and_a_blank_one_is_refused() -> None:
    first = instance.generate_env("owner@example.com", {})
    again = instance.generate_env("owner@example.com", first)

    assert again == first
    assert (first["ERRORS_DOMAIN"], first["ERRORS_PORT"]) == ("http://127.0.0.1:8210", "8210")
    with pytest.raises(instance.ErrorsError, match="ERRORS_SECRET_KEY empty"):
        instance.generate_env("owner@example.com", {**first, "ERRORS_SECRET_KEY": ""})


def test_it_runs_the_images_the_shared_glitchtip_pins_and_names_none_of_its_own() -> None:
    shared = yaml_subset.load(stack.COMPOSE.read_text())["services"]
    own = yaml_subset.load(instance.COMPOSE.read_text())["services"]
    made = instance.generate_env("owner@example.com", {})

    assert {key: made[key] for key in ("ERRORS_GLITCHTIP_IMAGE", "ERRORS_POSTGRES_IMAGE", "ERRORS_VALKEY_IMAGE")} == {
        "ERRORS_GLITCHTIP_IMAGE": shared["glitchtip"]["image"],
        "ERRORS_POSTGRES_IMAGE": shared["glitchtip-postgres"]["image"],
        "ERRORS_VALKEY_IMAGE": shared["glitchtip-valkey"]["image"],
    }
    assert all(service["image"].startswith("${ERRORS_") for service in own.values())


def test_its_project_address_comes_from_its_own_container_once_the_owner_and_organization_are_made() -> None:
    asked: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        assert stdin is not None
        line = next(text for text in stdin.splitlines() if text.startswith("PARAMS = json.loads("))
        params = json.loads(ast.literal_eval(line[len("PARAMS = json.loads(") : -1]))
        asked.append((argv, params))
        said = {"token": "t"} if params["action"] == "bootstrap" else {"dsns": {"factory": "http://k@127.0.0.1:8210/1"}}
        return subprocess.CompletedProcess(argv, 0, glitchtip.RESULT + json.dumps(said) + "\n", "")

    assert instance.dsn(run, "owner@example.com", "secret") == "http://k@127.0.0.1:8210/1"
    assert [params["action"] for _, params in asked] == ["bootstrap", "workspace"]
    assert all(argv[:4] == ["docker", "exec", "-i", instance.CONTAINER] for argv, _ in asked)
    assert all(params["org"] == "factory" for _, params in asked)


@pytest.mark.parametrize(
    ("address", "own"),
    [
        ("http://k@127.0.0.1:8210/1", True),
        ("http://k@127.0.0.1:8200/1", False),
        ("http://k@localhost:8210/1", False),
        ("http://k@[::1]:8200/1", False),
        ("http://k@factory-glitchtip:8000/1", False),
        ("https://k@notes-box.example.ts.net:8200/1", False),
    ],
    ids=["its-own", "the-laptops-shared", "by-another-name", "the-shared-on-ipv6", "inside-docker", "the-servers"],
)
def test_only_its_own_address_is_taken_for_the_factorys(address: str, own: bool) -> None:
    assert instance.is_own(address) is own


def _keying_job(tmp_path: Path, answers: dict[str, dict[str, object]]) -> Install:
    """An install at its keys step, deciding like a real one, with each GlitchTip's container answering as given."""
    job = dry_job(tmp_path)
    assert isinstance(job.host, DryHost)
    job.host.dry = False  # decides like a real install; a dry host still changes nothing
    for uid, user in ((1002, layout.STACK_USER), (1003, layout.ERRORS_USER)):
        home = tmp_path / "home" / user
        job.host.users[user] = Account(user, uid, uid, home, (user,), "")
    job.places.services.mkdir(parents=True)
    (job.places.services / stack.ENV_FILE).write_text("".join(f"{k}=v-{k}\n" for k in provision.SERVICE_KEYS))
    errors_env = job.places.errors / instance.ENV_FILE
    job.host.files[errors_env] = instance.render_env(instance.generate_env("owner@example.com", {}))
    for container, said in answers.items():
        job.host.answers[" ".join(provision.shell(container))] = (0, glitchtip.RESULT + json.dumps(said) + "\n")
    return job


@pytest.mark.parametrize(
    ("given", "refused"),
    [("http://k@127.0.0.1:8210/1", False), ("http://k@127.0.0.1:8200/1", True)],
    ids=["its-own-is-saved", "any-other-is-refused"],
)
def test_the_install_saves_the_factorys_own_address_and_refuses_any_other(
    tmp_path: Path, given: str, refused: bool
) -> None:
    said = {"token": "read-token", "dsns": {"factory": given}}
    job = _keying_job(tmp_path, {provision.GLITCHTIP: said, instance.CONTAINER: said})
    saved = f"write: {error_reports.dsn_path(job.places)} "

    if refused:
        with pytest.raises(Refused, match="127.0.0.1:8200 as its address, not its own http://127.0.0.1:8210"):
            steps_services.keys(job)
        assert not any(action.startswith(saved) for action in job.host.actions)
    else:
        steps_services.keys(job)
        assert any(action.startswith(saved) for action in job.host.actions)


def test_each_glitchtip_is_set_up_by_the_account_whose_docker_runs_it(tmp_path: Path) -> None:
    said = {"token": "read-token", "dsns": {"factory": "http://k@127.0.0.1:8210/1"}}
    job = _keying_job(tmp_path, {provision.GLITCHTIP: said, instance.CONTAINER: said})
    assert isinstance(job.host, DryHost)

    steps_services.keys(job)
    ran = [action for action in job.host.actions if action.startswith("run: docker exec -i ")]

    assert {action.split()[4]: action.rsplit("(as ", 1)[1] for action in ran} == {
        provision.GLITCHTIP: f"{layout.STACK_USER})",
        instance.CONTAINER: f"{layout.ERRORS_USER})",
    }


def test_factory_errors_reads_only_the_shared_glitchtip_and_never_the_factorys_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = {"token": "shared-token", "dsns": {}}
    own = {"token": "own-token", "dsns": {"factory": "http://own-key@127.0.0.1:8210/1"}}
    job = _keying_job(tmp_path, {provision.GLITCHTIP: shared, instance.CONTAINER: own})
    written: dict[Path, str] = {}

    def keep(path: Path, text: str, **_: object) -> None:
        written[path] = text
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    monkeypatch.setattr(job.host, "write", keep)
    steps_services.keys(job)
    read = telemetry.glitchtip(job.places, "slot1")

    assert (read.url, read.token) == (layout.GLITCHTIP_URL, "shared-token")
    assert all("own-token" not in text for text in written.values())
    assert [path for path, text in written.items() if "own-key" in text] == [error_reports.dsn_path(job.places)]


def test_a_reinstall_keeps_its_secrets_and_only_its_own_account_reads_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = dry_job(tmp_path)
    assert isinstance(job.host, DryHost)
    errors_env = job.places.errors / instance.ENV_FILE
    job.host.files[errors_env] = instance.render_env(instance.generate_env("owner@example.com", {}))
    before = stack.parse_env(job.host.files[errors_env], str(errors_env))
    written: dict[Path, tuple[str, dict[str, Any]]] = {}
    real_write = job.host.write

    def keep(path: Path, text: str, **how: Any) -> None:
        written[path] = (text, how)
        real_write(path, text, **how)

    monkeypatch.setattr(job.host, "write", keep)

    install.run(job)
    text, how = written[errors_env]

    after = stack.parse_env(text, str(errors_env))
    assert {key: after[key] for key in before if "PASSWORD" in key or "SECRET" in key} == {
        key: before[key] for key in before if "PASSWORD" in key or "SECRET" in key
    }
    assert (how["mode"], how["group"]) == (0o640, layout.ERRORS_USER)
    assert f"make: {job.places.errors}/ (750, root:{layout.ERRORS_USER})" in job.host.actions
