"""Root's upgrade unit: it trusts nothing the warden said, checking the release on GitHub and the file it downloads."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from runtime.shared import releases, store, upgrades
from runtime.shared.paths import Places
from runtime.system import release
from runtime.system.runners import upgrade

OLD = "a" * 40
ROOT = Path(__file__).resolve().parents[2]
API = "https://api.github.com/repos/acme/factory"
BOT = {"login": releases.CI_BOT}
STORAGE = "https://objects.example/release-asset"


def _release_file(tmp_path: Path) -> tuple[bytes, str]:
    repo = tmp_path / "factory"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@invalid"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (repo / "README.md").write_text("the factory\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "The first commit"], check=True)
    out = tmp_path / "dist" / release.NAME
    made = release.build(repo, "HEAD", "main", "acme/factory", out)
    return out.read_bytes(), str(made["commit"])


def _github(data: bytes, commit: str, seen: list[tuple[str, dict[str, str]]], **wrong: Any) -> releases.Get:
    tag = releases.tag(commit)
    digest = wrong.get("digest", hashlib.sha256(data).hexdigest())
    answers: dict[str, Any] = {
        f"{API}/releases/tags/{tag}": {"target_commitish": commit, "tag_name": tag, "author": BOT,
                                       "published_at": "2026-10-11T00:00:00Z",
                                       "assets": [{"name": releases.ASSET, "url": f"{API}/releases/assets/1",
                                                   "uploader": wrong.get("uploader", BOT)},
                                                  {"name": releases.CHECKSUM, "url": f"{API}/releases/assets/2",
                                                   "uploader": BOT}]},
        f"{API}/git/ref/tags/{tag}": {"object": {"sha": wrong.get("tagged", commit), "type": "commit"}},
        API: {"default_branch": "main"},
        f"{API}/compare/{OLD}...{commit}": {"status": wrong.get("newer", "ahead"), "commits": []},
        f"{API}/compare/{commit}...main": {"status": wrong.get("on", "identical"), "commits": []},
    }  # fmt: skip

    def get(url: str, headers: dict[str, str], timeout: float) -> releases.Answer:
        seen.append((url, headers))
        if url.startswith(f"{API}/actions/runs?"):
            return releases.Answer(200, {}, json.dumps({"total_count": wrong.get("runs", 1)}).encode())
        if url.endswith("/releases/assets/1") or url.endswith("/releases/assets/2"):
            return releases.Answer(302, {"location": f"{STORAGE}/{url[-1]}"}, b"")
        if url == f"{STORAGE}/1":
            return releases.Answer(200, {}, data)
        if url == f"{STORAGE}/2":
            return releases.Answer(200, {}, f"{digest}  {releases.ASSET}\n".encode())
        return releases.Answer(200, {}, json.dumps(answers[url]).encode())

    return get


def test_a_release_ci_made_newer_and_on_the_branch_is_fetched_and_its_file_checked(tmp_path: Path) -> None:
    data, commit = _release_file(tmp_path)
    seen: list[tuple[str, dict[str, str]]] = []
    rel = releases.Releases("acme/factory", "github_pat_x", 5, _github(data, commit, seen))
    folder = tmp_path / "download"
    folder.mkdir()

    path = upgrade.fetch(rel, upgrade.check(rel, OLD, commit), commit, folder)

    assert path.read_bytes() == data and oct(path.stat().st_mode & 0o777) == "0o700"
    storage = [headers for url, headers in seen if url.startswith(STORAGE)]
    assert len(storage) == 2 and all("Authorization" not in h for h in storage)  # the token stays with GitHub's API


@pytest.mark.parametrize(
    ("wrong", "says"),
    [({"runs": 0}, "CI has not passed"), ({"newer": "behind"}, "is not newer"),
     ({"on": "diverged"}, "is not on the factory's main"), ({"digest": "0" * 64}, "does not match its checksum"),
     ({"tagged": "f" * 40}, "does not point at"), ({"uploader": {"login": "someone"}}, "not uploaded by CI")],
    ids=["ci-did-not-pass", "older-than-installed", "off-the-default-branch", "file-not-as-published",
         "tag-moved-elsewhere", "file-uploaded-by-a-person"],
)  # fmt: skip
def test_a_release_it_cannot_trust_is_never_installed(tmp_path: Path, wrong: dict[str, Any], says: str) -> None:
    data, commit = _release_file(tmp_path)
    rel = releases.Releases("acme/factory", "github_pat_x", 5, _github(data, commit, [], **wrong))
    folder = tmp_path / "download"
    folder.mkdir()

    with pytest.raises((upgrade.Stop, releases.ReleaseError), match=says):
        upgrade.fetch(rel, upgrade.check(rel, OLD, commit), commit, folder)
    assert list(folder.iterdir()) == []


def test_the_whole_run_reads_the_shipped_defaults_and_hands_the_checked_file_to_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data, commit = _release_file(tmp_path)
    places = Places.under(tmp_path / "machine")
    places.install.mkdir(parents=True)
    shutil.copy(ROOT / "settings.toml", places.defaults)  # its repo.path and repo.github are the install's to set
    places.upgrade.mkdir(parents=True)
    store.write_json(
        upgrades.installed_path(places), {"commit": OLD, "owner": "o", "product": "/p", "repo": "acme/factory"}
    )
    upgrades.token_path(places).write_text("github_pat_x\n")
    monkeypatch.setattr(releases, "http_get", _github(data, commit, []))
    ran: list[list[str]] = []

    def run(argv: list[str], **how: object) -> subprocess.CompletedProcess[str]:
        ran.append(argv)
        assert Path(argv[2]).read_bytes() == data  # the checked file, where only root may write
        upgrades.write_progress(places, OLD, commit, upgrades.DONE, "")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(upgrade.subprocess, "run", run)

    assert upgrade.upgrade(places, commit) == 0
    assert [argv[3:] for argv in ran] == [["--upgrade"]]
    said = upgrades.read_progress(places)
    assert said is not None and said["state"] == upgrades.DONE
