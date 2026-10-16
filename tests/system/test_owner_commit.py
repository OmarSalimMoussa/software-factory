"""factory-own: root commits the owner's own change on the agents' default branch and records it, so the warden's push
lets exactly that through."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from runtime.queue import turn
from runtime.shared import upgrades
from runtime.shared.paths import Places
from runtime.system import owner_commit, root_commits
from runtime.system.host import CommandError, Host
from runtime.system.journal import Journal
from runtime.system.real_host import RealHost
from runtime.warden.delivery import pins

TOML = '[product]\nname = "shop"\n'


def git(repo: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The agents' checkout of a product, with this test's user standing in for the agent's."""
    monkeypatch.setattr(root_commits, "AGENT_USER", "")
    monkeypatch.setattr(owner_commit, "AGENT_USER", "")
    repo = tmp_path / "agents"
    repo.mkdir()
    (repo / "factory.toml").write_text(TOML)
    (repo / "app.py").write_text("print('shop')\n")
    git(repo, "init", "-q", "-b", "master")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-m", "The shop")
    return repo


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> Host:
    found = RealHost(Journal(terminal=False))
    monkeypatch.setattr(found, "write", lambda path, text, **how: (path.parent.mkdir(parents=True, exist_ok=True),
                                                                   path.write_text(text)))  # fmt: skip
    return found


def patch_of(tmp_path: Path, agents: Path, change: Callable[[Path], None], renames: bool = False) -> Path:
    """The owner's change, made in their own clone and written out by `git format-patch`, as the docs say unless
    ``renames``."""
    mine = tmp_path / "mine"
    subprocess.run(["git", "clone", "-q", str(agents), str(mine)], check=True)
    change(mine)
    git(mine, "add", "-A")
    git(mine, "-c", "user.name=Owner", "-c", "user.email=owner@shop.invalid", "commit", "-q", "-m",
        "The factory reads the reports\n\nfactory.toml names the export.")  # fmt: skip
    out = tmp_path / "change.patch"
    out.write_text(git(mine, "format-patch", "-1", *([] if renames else ["--no-renames"]), "--binary", "--stdout"))
    return out


def reports(root: Path) -> None:
    (root / "factory.toml").write_text(TOML + "\n[reports]\nport = 8110\n")
    (root / "app.py").write_text("print('shop, with reports')\n")


def test_the_owners_patch_lands_as_one_commit_of_theirs_that_the_push_lets_through(
    tmp_path: Path, agents: Path, host: Host
) -> None:
    places, before = Places.under(tmp_path / "root"), git(agents, "rev-parse", "HEAD").strip()
    patch = patch_of(tmp_path, agents, reports)

    made = owner_commit.from_patch(host, places, agents, "master", patch)

    assert git(agents, "rev-parse", "HEAD").strip() == made and git(agents, "rev-parse", "HEAD^").strip() == before
    assert git(agents, "log", "-1", "--format=%an <%ae>|%cn|%B").strip() == (
        f"Owner <owner@shop.invalid>|{root_commits.GIT_NAME}|The factory reads the reports\n\n"
        "factory.toml names the export."
    )
    assert (agents / "factory.toml").read_text() == TOML + "\n[reports]\nport = 8110\n"
    assert git(agents, "status", "--porcelain") == ""
    [record] = upgrades.read_owned(places)
    assert (record["commit"], record["parent"]) == (made, before)
    assert pins.allowed(lambda *argv: git(agents, *argv), places, before, made) == {"factory.toml", "app.py"}


def _rename(root: Path) -> None:
    git(root, "mv", "app.py", "main.py")


def _executable(root: Path) -> None:
    (root / "app.py").chmod(0o755)


def _binary(root: Path) -> None:
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")


@pytest.mark.parametrize(
    ("change", "before", "said"),
    [
        (_rename, "renames", "renames, copies or changes a mode"),
        (_executable, None, "renames, copies or changes a mode"),
        (_binary, None, "holds a binary file, logo.png"),
        (reports, lambda agents: (agents / "app.py").write_text("print('half done')\n"), "changes of its own"),
        (reports, lambda agents: None, "holds the merge turn"),
        (reports, "windows", "Windows line ends"),
        (reports, "two-commits", "holds 2 commits"),
    ],
    ids=[
        "a-rename",
        "an-executable",
        "a-binary",
        "changes-of-its-own",
        "the-merge-turn",
        "windows-line-ends",
        "two-commits",
    ],  # fmt: skip
)
def test_root_refuses_a_patch_it_cannot_carry_over_as_written_and_changes_nothing(
    tmp_path: Path, agents: Path, host: Host, monkeypatch: pytest.MonkeyPatch,
    change: Callable[[Path], None], before: Callable[[Path], None] | str | None, said: str,
) -> None:  # fmt: skip
    places = Places.under(tmp_path / "root")
    if before == "windows":
        (agents / "app.py").write_bytes(b"print('shop')\r\n")
        git(agents, "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-am", "Windows")
    patch = patch_of(tmp_path, agents, change, renames=before == "renames")
    if before == "two-commits":  # `git apply` would apply both under the first one's message
        (tmp_path / "mine" / "app.py").write_text("print('two')\n")
        git(tmp_path / "mine", "-c", "user.name=O", "-c", "user.email=o@invalid", "commit", "-q", "-am", "Two")
        patch.write_text(patch.read_text() + git(tmp_path / "mine", "format-patch", "-1", "--stdout"))
    if callable(before):
        before(agents)
    if said == "holds the merge turn":
        monkeypatch.setattr(turn, "holder", lambda places: {"task": "t-0001", "branch": "b", "base": "c"})
    head = git(agents, "rev-parse", "HEAD")

    with pytest.raises(owner_commit.Refusal, match=said):
        owner_commit.from_patch(host, places, agents, "master", patch)

    assert git(agents, "rev-parse", "HEAD") == head and upgrades.read_owned(places) == []


def test_a_replace_ref_the_agent_planted_never_shapes_what_root_commits(
    tmp_path: Path, agents: Path, host: Host
) -> None:
    places, patch = Places.under(tmp_path / "root"), patch_of(tmp_path, agents, reports)
    git(agents, "checkout", "-q", "-b", "fake")
    (agents / "factory.toml").write_text(TOML + "# the agent's own line\n")
    git(agents, "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-am", "Fake")
    fake = git(agents, "rev-parse", "HEAD^{tree}").strip()
    git(agents, "checkout", "-q", "master")
    git(agents, "replace", git(agents, "rev-parse", "HEAD^{tree}").strip(), fake)
    git(agents, "read-tree", "-u", "--reset", "HEAD")  # the files show the fake tree; the agent's git sees no change
    assert git(agents, "status", "--porcelain", "--untracked-files=no") == ""

    with pytest.raises(owner_commit.Refusal, match="changes of its own"):
        owner_commit.from_patch(host, places, agents, "master", patch)
    assert upgrades.read_owned(places) == []


def test_a_script_the_owner_changes_stays_executable(tmp_path: Path, agents: Path, host: Host) -> None:
    (agents / "run.sh").write_text("echo shop\n")
    (agents / "run.sh").chmod(0o755)
    git(agents, "add", "run.sh")
    git(agents, "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "-m", "A script")
    patch = patch_of(tmp_path, agents, lambda root: (root / "run.sh").write_text("echo shop, with reports\n"))

    owner_commit.from_patch(host, Places.under(tmp_path / "root"), agents, "master", patch)

    assert git(agents, "ls-tree", "HEAD", "run.sh").split()[0] == "100755"


def test_a_commit_root_made_is_recorded_before_the_branch_holds_it(
    tmp_path: Path, agents: Path, host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    places, run = Places.under(tmp_path / "root"), host.run

    def files_refuse(argv: list[str], **how: Any) -> str:
        if argv[-4:-2] == ["-m", "-u"]:  # the files follow the branch last; the agent may have touched one
            raise CommandError(argv, 128, "Entry 'app.py' not uptodate")
        return run(argv, **how)

    monkeypatch.setattr(host, "run", files_refuse)
    with pytest.raises(CommandError):
        owner_commit.from_patch(host, places, agents, "master", patch_of(tmp_path, agents, reports))

    assert [r["commit"] for r in upgrades.read_owned(places)] == [git(agents, "rev-parse", "master").strip()]


def test_a_file_moved_in_a_patch_without_renames_lands_as_a_removal_and_a_new_file(
    tmp_path: Path, agents: Path, host: Host
) -> None:
    owner_commit.from_patch(
        host, Places.under(tmp_path / "root"), agents, "master", patch_of(tmp_path, agents, _rename)
    )

    assert not (agents / "app.py").exists() and (agents / "main.py").read_text() == "print('shop')\n"
    assert git(agents, "status", "--porcelain") == ""


def test_a_change_already_on_the_branch_is_recorded_as_the_owners_but_never_a_merge_or_another_branchs(
    tmp_path: Path, agents: Path, host: Host
) -> None:
    places, before = Places.under(tmp_path / "root"), git(agents, "rev-parse", "HEAD").strip()
    reports(agents)
    git(agents, "-c", "user.name=Owner", "-c", "user.email=o@invalid", "commit", "-q", "-am", "Reports")
    made = git(agents, "rev-parse", "HEAD").strip()

    assert owner_commit.from_commit(host, places, agents, "master", made[:9]) == made
    assert pins.allowed(lambda *argv: git(agents, *argv), places, before, made) == {"factory.toml", "app.py"}
    with pytest.raises(owner_commit.Refusal, match="already recorded"):
        owner_commit.from_commit(host, places, agents, "master", made)
    git(agents, "checkout", "-q", "-b", "side")
    git(agents, "-c", "user.name=t", "-c", "user.email=t@invalid", "commit", "-q", "--allow-empty", "-m", "Side")
    side = git(agents, "rev-parse", "HEAD").strip()
    git(agents, "checkout", "-q", "master")
    with pytest.raises(owner_commit.Refusal, match="is not on master"):
        owner_commit.from_commit(host, places, agents, "master", side)
    git(agents, "-c", "user.name=t", "-c", "user.email=t@invalid", "merge", "-q", "--no-ff", "-m", "Merge side", "side")
    with pytest.raises(owner_commit.Refusal, match="is a merge"):
        owner_commit.from_commit(host, places, agents, "master", "HEAD")
    assert len(upgrades.read_owned(places)) == 1


def test_factory_own_runs_only_as_root_from_the_owners_sudo(capsys: pytest.CaptureFixture[str]) -> None:
    assert owner_commit.main(["--commit", "HEAD"]) == 2
    assert "run it with sudo" in capsys.readouterr().err
