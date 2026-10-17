"""Root's upgrade unit: it downloads any commit of the factory from GitHub with root's own token and installs it from a
sealed copy, older commits too, so the commit before is the way back."""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from runtime.shared import network, store, upgrades
from runtime.shared.paths import Places
from runtime.system.runners import upgrade

OLD = "a" * 40
ROOT = Path(__file__).resolve().parents[2]
API = "https://api.github.com/repos/acme/factory"
STORAGE = "https://codeload.example/acme/factory/legacy.tar.gz"


def _commit(tmp_path: Path) -> tuple[bytes, str]:
    """A commit of a small factory and GitHub's tarball of it, which git archive makes as GitHub does."""
    repo = tmp_path / "factory"
    (repo / "runtime" / "system").mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@invalid"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (repo / "runtime" / "system" / "install.py").write_text("print('installing')\n")
    (repo / "README.md").write_text("the factory\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "The first commit"], check=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    made = subprocess.run([*git, "archive", "--format=tar.gz", f"--prefix=acme-factory-{sha[:7]}/", sha],
                          capture_output=True, check=True)  # fmt: skip
    return made.stdout, sha


def _tarball(comment: str, *members: tuple[str, str]) -> bytes:
    """A tarball by hand: each member a file of that text, or a link when the text starts with ->."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": comment}) as tar:
        for name, text in members:
            info = tarfile.TarInfo(name)
            if text.startswith("->"):
                info.type, info.linkname = tarfile.SYMTYPE, text[2:]
                tar.addfile(info)
            else:
                info.size = len(text)
                tar.addfile(info, io.BytesIO(text.encode()))
    return out.getvalue()


def _github(data: bytes, sha: str, seen: list[tuple[str, dict[str, str]]], status: int = 302,
            where: str = STORAGE) -> upgrade.factory_repo.Get:  # fmt: skip
    def get(url: str, headers: dict[str, str], timeout: float) -> network.Answer:
        seen.append((url, headers))
        if url == f"{API}/tarball/{sha}":
            return network.Answer(status, {"location": where}, b"")
        assert url == where
        return network.Answer(200, {}, data)

    return get


def _machine(tmp_path: Path) -> Places:
    places = Places.under(tmp_path / "machine")
    places.install.mkdir(parents=True)
    shutil.copy(ROOT / "settings.toml", places.defaults)  # its repo.path and repo.github are the install's to set
    places.upgrade.mkdir(parents=True)
    store.write_json(
        upgrades.installed_path(places), {"commit": OLD, "owner": "o", "product": "/p", "repo": "acme/factory"}
    )
    upgrades.token_path(places).write_text("github_pat_x\n")
    return places


def test_any_commit_is_downloaded_with_roots_token_and_installed_from_a_sealed_copy_that_goes_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, sha = _commit(tmp_path)
    places, stages, seen = _machine(tmp_path), tmp_path / "stages", []
    ran: list[tuple[str, Path, str, list[str]]] = []

    def run_staged(commit: str, stage: Path, script: str, argv: list[str]) -> int:
        ran.append((commit, stage, script, argv))
        assert (stage / script).read_text() == "print('installing')\n" and (stage / "README.md").exists()
        assert not (stage / script).stat().st_mode & 0o222 and not stage.stat().st_mode & 0o222  # read-only
        upgrades.write_progress(places, OLD, commit, upgrades.DONE, "")
        return 0

    monkeypatch.setattr(upgrade.sealed, "run_staged", run_staged)

    assert upgrade.upgrade(places, sha, _github(data, sha, seen), stages) == 0

    assert [(c, s.parent, s.name[:41], script, argv) for c, s, script, argv in ran] == [
        (sha, stages, f"{sha}-", "runtime/system/install.py", ["--commit", sha, "--upgrade"])
    ]
    assert list(stages.iterdir()) == []  # the sealed copy goes once its install ended
    assert [h.get("Authorization") for _, h in seen] == ["Bearer github_pat_x", None]  # the token stays with the API
    said = upgrades.read_progress(places)
    assert said is not None and said["state"] == upgrades.DONE


@pytest.mark.parametrize(
    ("tarball", "github", "says"),
    [
        (lambda sha: _tarball("f" * 40, ("acme-factory/README.md", "x")), {}, "GitHub sent no tarball of"),
        (lambda sha: _tarball(sha, ("one/README.md", "x"), ("two/README.md", "y")), {}, "GitHub sent no tarball of"),
        (lambda sha: _tarball(sha, ("acme-factory/out", "->../../../etc/passwd")), {}, "cannot be unpacked"),
        (lambda sha: b"not a tarball", {}, "cannot be unpacked"),
        (lambda sha: b"", {"status": 404}, "Contents: Read on the factory repo"),
        (lambda sha: b"", {"where": "http://codeload.example/plain"}, "no https address"),
    ],
    ids=["another-commit", "two-top-folders", "a-link-out-of-the-copy", "not-a-tarball", "token-cannot-read",
         "storage-not-https"],
)  # fmt: skip
def test_what_github_sends_that_is_not_the_commit_is_never_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tarball: Callable[[str], bytes], github: dict[str, Any], says: str
) -> None:
    sha = "c" * 40
    places, stages = _machine(tmp_path), tmp_path / "stages"
    monkeypatch.setattr(upgrade.sealed, "run_staged", lambda *a: pytest.fail("an install ran"))
    get = _github(tarball(sha), sha, [], **github)

    with pytest.raises((upgrade.Stop, upgrade.factory_repo.RepoError), match=says):
        upgrade.upgrade(places, sha, get, stages)
    assert not stages.exists() or list(stages.iterdir()) == []


def test_a_stopped_upgrade_reports_an_unreadable_install_record_and_still_says_it_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    places = Places.under(tmp_path)
    places.upgrade.mkdir(parents=True)
    upgrades.installed_path(places).write_text("{}")  # a record with none of its keys
    monkeypatch.setattr(upgrade.Places, "system", classmethod(lambda cls: places))

    def stopped(places: Places, commit: str, get: object, stages: Path) -> int:
        raise upgrade.Stop("GitHub did not answer")

    monkeypatch.setattr(upgrade, "upgrade", stopped)

    assert upgrade.main(["b" * 40]) == 1

    assert "the install record cannot be read" in capsys.readouterr().err
    said = upgrades.read_progress(places)
    assert said is not None and (said["state"], said["from"], said["problem"]) == (
        upgrades.FAILED,
        "",
        "GitHub did not answer",
    )
