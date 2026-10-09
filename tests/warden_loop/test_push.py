from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from loop_fakes import World

from checks.runner import passes
from runtime.shared import models, store
from runtime.shared.models import Probe
from runtime.shared.protocol import Refused
from runtime.warden import github_push, inbox, ops, probes


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(checkout: Path, change: Callable[[Path], None]) -> str:
    change(checkout)
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "change")
    return git(checkout, "rev-parse", "HEAD")


def write(rel: str) -> Callable[[Path], None]:
    def change(root: Path) -> None:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("changed\n")

    return change


def move(src: str, dst: str) -> Callable[[Path], None]:
    def change(root: Path) -> None:
        (root / dst).parent.mkdir(parents=True, exist_ok=True)
        git(root, "mv", src, dst)

    return change


@pytest.fixture
def github_repo(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A bare repo standing in for GitHub, reached through the real url, and the agent's checkout cloned from it."""
    remote = tmp_path / "github.git"
    config = tmp_path / "gitconfig"
    config.write_text(
        f'[url "{remote}"]\n\tinsteadOf = git@github.com:owner/name.git\n'
        "[user]\n\tname = Tester\n\temail = tester@example.invalid\n[init]\n\tdefaultBranch = master\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(tmp_path, "init", "-q", "--bare", str(remote))
    world.checkout.mkdir()
    git(world.checkout, "init", "-q")
    commit(world.checkout, write(".github/workflows/ci.yml"))
    git(world.checkout, "push", "-q", str(remote), "master")
    key = github_push.deploy_key(world.ctx.places)
    key.parent.mkdir(parents=True)
    key.write_text("not a real key\n")
    return remote


@pytest.mark.parametrize(
    "change",
    [
        write(".github/workflows/deploy.yml"),
        write("scripts/ci/promote"),
        write("check-values/surfaces.toml"),
        write("GOAL.md"),
        write("goal/G1.md"),
        write('.github/workflows/a"b\tc.yml'),
        move(".github/workflows/ci.yml", "docs/ci.yml"),
    ],
    ids=["workflow", "ci-script", "surfaces", "goal-file", "goal-folder", "quoted-name", "renamed-away"],
)
def test_push_refuses_a_range_touching_a_protected_path(
    world: World, github_repo: Path, change: Callable[[Path], None]
) -> None:
    before = git(github_repo, "rev-parse", "master")
    commit(world.checkout, write("src/app.py"))
    sha = commit(world.checkout, change)
    with pytest.raises(Refused) as refused:
        github_push.push_checked(world.ctx, {"sha": sha}, None)
    assert refused.value.rule == "protected-path"
    assert git(github_repo, "rev-parse", "master") == before


def test_push_sends_only_a_commit_whose_tree_passed_the_checks(world: World, github_repo: Path) -> None:
    checked = commit(world.checkout, write("src/app.py"))
    commit(world.checkout, write("src/later.py"))
    with pytest.raises(Refused) as refused:
        github_push.push_checked(world.ctx, {"sha": checked}, None)
    assert refused.value.rule == "unchecked"

    passes.record(world.checkout, git(world.checkout, "rev-parse", f"{checked}^{{tree}}"), "master", "now")
    answer = github_push.push_checked(world.ctx, {"sha": checked}, None)
    assert answer["commits"] == 1
    assert git(github_repo, "rev-parse", "master") == checked


def probe(world: World, env: str, ok: bool, commit: str = "") -> None:
    error = "" if ok else "HTTP 502"
    record = Probe(world.clock.iso(), env, ok, commit, 40, error, counts=not ok)
    store.append_jsonl(world.ctx.places.probes, models.dump(record))


STAGING, PROD = "a" * 40, "b" * 40


@pytest.mark.parametrize(
    ("staging_ok", "expected", "rule"),
    [(True, "c" * 40, "stale"), (False, STAGING, "staging-red")],
    ids=["stale-sha", "staging-red"],
)
def test_push_to_prod_refuses_a_stale_sha_or_a_red_staging(
    world: World, staging_ok: bool, expected: str, rule: str
) -> None:
    probe(world, probes.STAGING, True, STAGING)
    probe(world, probes.STAGING, staging_ok, STAGING if staging_ok else "")
    with pytest.raises(Refused) as refused:
        github_push.push_prod(world.ctx, {"expected_staging_sha": expected}, None)
    assert refused.value.rule == rule
    assert world.github.dispatched == []


def test_push_to_prod_runs_its_steps_and_asks_for_a_qa_prod_pass(world: World) -> None:
    probe(world, probes.STAGING, True, STAGING)
    probe(world, probes.PROD, True, PROD)
    world.github.on_run = lambda: probe(world, probes.PROD, True, STAGING)  # the loop sees prod on the new build
    started = github_push.push_prod(world.ctx, {"expected_staging_sha": STAGING}, None)
    ops.join(started["op"], timeout=30)
    op = ops.get(world.ctx, {"id": started["op"]}, None)["op"]
    assert [step["state"] for step in op["steps"]] == [ops.DONE] * 4, op
    assert world.github.dispatched == [STAGING]
    assert world.events == [(inbox.QA_PROD_DUE, {"sha": STAGING, "kind": "push"})]
    assert github_push.watching(world.ctx)
