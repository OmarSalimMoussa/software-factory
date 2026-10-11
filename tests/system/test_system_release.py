"""The release file: built from one commit, it checks its own hashes before it does anything, runs only as root, and
starts the console, the terminal installer or the uninstaller from a read-only stage that carries the factory's
history."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import Any, NoReturn

import pytest

from runtime.system import release, sealed

COMMIT = ["-c", "user.name=Owner", "-c", "user.email=owner@example.com", "-c", "commit.gpgsign=false"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _factory(tmp_path: Path) -> Path:
    """A small factory repository: a file, a program and a link to it, in one commit on main."""
    repo = tmp_path / "factory"
    (repo / "bin").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("the factory\n", encoding="utf-8")
    (repo / "bin" / "run").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "bin" / "run").chmod(0o755)
    (repo / "bin" / "again").symlink_to("run")
    _git(repo, "add", ".")
    _git(repo, *COMMIT, "commit", "-q", "-m", "The first commit")
    return repo


def _built(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    out = tmp_path / "dist" / release.NAME
    return out, release.build(_factory(tmp_path), "HEAD", "main", "acme/factory", out)


def _changed(path: Path, member: str, data: bytes) -> None:
    """``path`` with ``member`` replaced, or added, and everything else as it was."""
    with zipfile.ZipFile(path) as old:
        kept = [(info, old.read(info)) for info in old.infolist() if info.filename != member]
    with path.open("wb") as fh:
        fh.write(b"#!/usr/bin/env python3\n")
        with zipfile.ZipFile(fh, "w") as new:
            for info, body in kept:
                new.writestr(info, body)
            new.writestr(member, data)


@pytest.mark.parametrize(
    ("member", "data"),
    [(None, b""), ("factory/README.md", b"changed\n"), ("factory/extra.txt", b"new\n"), ("factory.bundle", b"junk"),
     ("__main__.py", b"print('mine')\n")],
    ids=["whole", "a-changed-file", "an-added-file", "an-added-member", "changed-start"],
)  # fmt: skip
def test_the_release_file_checks_its_own_hashes_and_refuses_a_changed_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], member: str | None, data: bytes
) -> None:
    out, made = _built(tmp_path)
    if member is not None:
        _changed(out, member, data)

    code = release.launch(str(out), ["--check"])

    said = capsys.readouterr()
    if member is None:
        assert code == 0 and said.out.splitlines()[-1] == "Its files match its manifest."
        assert said.out.startswith(f"software-factory {made['commit'][:12]} (The first commit), built ")
        digest = hashlib.sha256(out.read_bytes()).hexdigest()
        assert (out.parent / f"{release.NAME}.sha256").read_text(encoding="utf-8") == f"{digest}  {release.NAME}\n"
        return
    assert code == 1 and said.err.startswith("This release file is damaged: ") and not said.out


@pytest.mark.parametrize(
    ("argv", "script", "passed", "display"),
    [
        (["--repo", "/home/owner/shop"], "console.py", ["--repo", "/home/owner/shop"], sealed.DISPLAY),
        (["--terminal", "--repo", "/home/owner/shop", "--set", "caps.implementers=2", "--verbose"], "install.py",
         ["--repo", "/home/owner/shop", "--set", "caps.implementers=2", "--verbose"], ()),
        (["--uninstall", "--dry-run"], "uninstall.py", ["--dry-run"], ()),
    ],
    ids=["console", "terminal", "uninstall"],
)  # fmt: skip
def test_run_with_sudo_it_starts_its_program_from_a_new_sealed_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], script: str, passed: list[str],
    display: tuple[str, ...],
) -> None:  # fmt: skip
    out, made = _built(tmp_path)
    monkeypatch.setattr(release.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "owner")
    monkeypatch.setattr(release.layout, "STAGE_ROOT", tmp_path / "stages")
    started: list[tuple[str, Path, str, list[str], Path | None, tuple[str, ...]]] = []

    def exec_staged(sha: str, stage: Path, program: str, given: list[str], *, release: Path | None,
                    display: tuple[str, ...]) -> NoReturn:  # fmt: skip
        started.append((sha, stage, program, given, release, display))
        raise SystemExit(0)

    monkeypatch.setattr(sealed, "exec_staged", exec_staged)

    with pytest.raises(SystemExit):
        release.launch(str(out), argv)

    [(sha, stage, program, given, folder, kept)] = started
    commit = [] if script == "uninstall.py" else ["--commit", made["commit"]]
    assert (sha, program, given, kept) == (made["commit"], f"runtime/system/{script}", [*commit, *passed], display)
    assert stage.parent == tmp_path / "stages" and folder == sealed.release_folder(stage)


def test_the_release_file_runs_nothing_without_sudo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    out, _ = _built(tmp_path)
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setattr(release.layout, "STAGE_ROOT", tmp_path / "stages")

    assert release.launch(str(out), []) == 2
    assert "sudo" in capsys.readouterr().err and not (tmp_path / "stages").exists()


def test_a_stage_from_a_release_is_read_only_and_carries_its_manifest(tmp_path: Path) -> None:
    out, made = _built(tmp_path)

    stage = release.seal(release.Archive.load(out), tmp_path / "stages")

    modes = {path.relative_to(stage).as_posix(): stat.S_IMODE(os.lstat(path).st_mode) for path in stage.rglob("*")}
    assert {name: mode for name, mode in modes.items() if not (stage / name).is_symlink()} == {
        "README.md": 0o444, "bin": 0o555, "bin/run": 0o555,
    }  # fmt: skip
    assert os.readlink(stage / "bin" / "again") == "run" and stat.S_IMODE(stage.stat().st_mode) == 0o555
    folder = sealed.release_folder(stage)
    assert release.read_manifest(folder) == made
