"""The sealed copy an install and the console run from, and what every command they start sees of the environment."""

from __future__ import annotations

import getpass
import subprocess
from pathlib import Path
from typing import Any, NoReturn

import pytest
from system_fakes import SHA

from runtime.shared import environment
from runtime.shared.protocol import Refused
from runtime.system import layout, sealed
from runtime.system.host import DryHost, RealHost, user_env

STAGE = layout.STAGE_ROOT / f"{SHA}-0a1b2c3d"
CHECKOUT = Path("/home/owner/software-factory")


def test_a_sealed_copy_is_unpacked_by_root_into_a_new_folder_and_made_read_only_before_anything_runs() -> None:
    host = DryHost(answers={f"git -C {CHECKOUT} rev-parse --verify main^{{commit}}": (0, SHA + "\n")})

    sha, stage = sealed.stage_commit(host, "main", "owner", CHECKOUT)
    _, again = sealed.stage_commit(host, "main", "owner", CHECKOUT)

    assert sha == SHA and stage.parent == layout.STAGE_ROOT and stage.name.startswith(f"{SHA}-") and again != stage
    # The owner only reads their checkout; root alone makes, fills and seals the copy.
    assert host.actions[:4] == [
        f"make: {layout.STAGE_ROOT}/ (755, root:root)",
        f"make: {stage}/ (755, root:root)",
        f"run: git -C {CHECKOUT} archive --format=tar {SHA} (as owner) | "
        f"tar -x -f - -C {stage} --no-same-owner --no-same-permissions",
        f"run: chmod -R a=rX {stage}",
    ]


@pytest.mark.parametrize("display", [(), environment.DISPLAY], ids=["install", "console"])
def test_a_sealed_copy_starts_isolated_with_only_the_declared_environment(
    monkeypatch: pytest.MonkeyPatch, display: tuple[str, ...]
) -> None:
    inherited = {
        "PATH": "/home/owner/.venv/bin:/usr/bin", "PYTHONPATH": "/home/owner/src", "VIRTUAL_ENV": "/home/owner/.venv",
        "UV_PYTHON": "3.14", "SUDO_USER": "owner", "TERM": "xterm-256color", "DISPLAY": ":0",
        "WAYLAND_DISPLAY": "wayland-0", "XAUTHORITY": "/run/user/1000/xauth",
        environment.STAGE: "/tmp/elsewhere", environment.SHA: "junk", environment.CHECKOUT: "/tmp/junk",
    }  # fmt: skip
    for key, value in inherited.items():
        monkeypatch.setenv(key, value)
    started: list[tuple[str, list[str], dict[str, str]]] = []

    def execve(path: str, argv: list[str], env: dict[str, str]) -> NoReturn:
        started.append((path, argv, env))
        raise SystemExit(0)

    monkeypatch.setattr(sealed.os, "execve", execve)

    with pytest.raises(SystemExit):
        sealed.exec_staged(SHA, STAGE, "runtime/system/install.py", ["--commit", SHA], checkout=CHECKOUT,
                           display=display)  # fmt: skip

    [(program, argv, env)] = started
    assert [program, *argv] == [layout.PYTHON, layout.PYTHON, "-I", "-B", f"{STAGE}/runtime/system/install.py",
                                "--commit", SHA]  # fmt: skip
    assert env == {
        "PATH": layout.ROOT_PATH, "LANG": "C.UTF-8", "HOME": "/root", "SUDO_USER": "owner", "TERM": "xterm-256color",
        environment.STAGE: str(STAGE), environment.SHA: SHA, environment.CHECKOUT: str(CHECKOUT),
        **{key: inherited[key] for key in display},
    }  # fmt: skip


@pytest.mark.parametrize("as_owner", [False, True], ids=["as-root", "as-another-user"])
def test_a_command_the_installer_starts_sees_only_what_it_declares(
    monkeypatch: pytest.MonkeyPatch, as_owner: bool
) -> None:
    monkeypatch.setenv("PATH", "/home/owner/.venv/bin:/usr/bin")
    monkeypatch.setenv("PYTHONPATH", "/home/owner/src")
    started: list[tuple[list[str], dict[str, str]]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        started.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    me = getpass.getuser()
    host = RealHost()

    host.run(["git", "status"], user=me if as_owner else "", env={"GIT_DIR": "/srv/repo"})

    [(argv, env)] = started
    assert env["PATH"] == layout.ROOT_PATH
    if as_owner:
        account = host.account(me)
        assert account is not None
        declared = {**user_env(account), "GIT_DIR": "/srv/repo"}
        # env -i: nothing of root's environment reaches the user's command, the installer's own variables included.
        assert argv == ["runuser", "-u", me, "--", "env", "-i", *(f"{k}={v}" for k, v in declared.items()),
                        "git", "status"]  # fmt: skip
    else:
        assert argv == ["git", "status"] and env["GIT_DIR"] == "/srv/repo"


@pytest.mark.parametrize(
    ("stage", "kept", "removed"),
    [(STAGE, False, True), (STAGE, True, False), (Path("/home/owner"), False, None)],
    ids=["ended", "kept-beside-a-failed-install", "not-a-sealed-copy"],
)
def test_the_sealed_copy_goes_when_its_program_ends_unless_kept(stage: Path, kept: bool, removed: bool | None) -> None:
    host = DryHost(paths={sealed.release_folder(stage)})  # a release's manifest and history go with its copy
    host.dry = False  # decides like a real run; a dry host still changes nothing

    if removed is None:
        with pytest.raises(Refused, match="not a sealed copy"):
            sealed.finish(host, stage, kept=kept)
        assert host.actions == []
        return
    sealed.finish(host, stage, kept=kept)

    assert (f"remove: {stage}" in host.actions) is removed
    assert (f"remove: {sealed.release_folder(stage)}" in host.actions) is removed


@pytest.mark.parametrize(
    ("variables", "known"),
    [
        ({}, None),
        (
            {environment.STAGE: "/stage", environment.SHA: "abc", environment.CHECKOUT: "/checkout"},
            environment.Staged(Path("/stage"), "abc", Path("/checkout"), None),
        ),
        (
            {environment.STAGE: "/stage", environment.SHA: "abc", environment.RELEASE: "/release"},
            environment.Staged(Path("/stage"), "abc", None, Path("/release")),
        ),
    ],
    ids=["run-from-a-checkout", "sealed-from-a-checkout", "sealed-from-a-release"],
)
def test_a_process_knows_the_sealed_copy_it_runs_from(
    monkeypatch: pytest.MonkeyPatch, variables: dict[str, str], known: environment.Staged | None
) -> None:
    for key in (environment.STAGE, environment.SHA, environment.CHECKOUT, environment.RELEASE):
        monkeypatch.delenv(key, raising=False)
    for key, value in variables.items():
        monkeypatch.setenv(key, value)
    assert environment.staged() == known
