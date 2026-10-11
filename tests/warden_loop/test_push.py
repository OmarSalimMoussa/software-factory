from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from loop_fakes import World

from checks.runner import passes
from runtime.shared import models, store
from runtime.shared.models import Probe
from runtime.shared.protocol import Refused
from runtime.warden import deploys, github_push, inbox, ops, probes

ROOT = Path(__file__).resolve().parents[2]


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
    world.checkout.mkdir(exist_ok=True)
    git(world.checkout, "init", "-q")
    # The settings name the hook scripts a push refuses to change.
    shutil.copy(ROOT / "testdata" / "fixture-product" / "factory.toml", world.checkout / "factory.toml")
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
        write("factory.toml"),
        write("scripts/smoke-check"),
        write("check-values/surfaces.toml"),
        write("GOAL.md"),
        write("goal/G1.md"),
        write('.github/workflows/a"b\tc.yml'),
        move(".github/workflows/ci.yml", "docs/ci.yml"),
    ],
    ids=["workflow", "settings", "hook", "surfaces", "goal-file", "goal-folder", "quoted-name", "renamed-away"],
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


def test_push_reads_the_agents_checkout_though_another_user_owns_it(
    world: World, github_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked = commit(world.checkout, write("src/app.py"))
    passes.record(world.checkout, git(world.checkout, "rev-parse", f"{checked}^{{tree}}"), "master", "now")
    # Git's own test switch makes every repository another user's; the global config trusts the warden's mirror and
    # GitHub's stand-in, never the agent's checkout.
    config = github_repo.parent / "gitconfig"
    mirror = github_push.mirror_path(world.ctx.places)
    config.write_text(config.read_text() + f"[safe]\n\tdirectory = {mirror}\n\tdirectory = {github_repo}\n")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

    answer = github_push.push_checked(world.ctx, {"sha": checked}, None)

    assert answer["commits"] == 1


def probe(world: World, env: str, ok: bool, commit: str = "") -> None:
    error = "" if ok else "HTTP 502"
    record = Probe(world.clock.iso(), env, ok, commit, 40, error, counts=not ok)
    store.append_jsonl(world.ctx.places.probes, models.dump(record))


STAGING, PROD, OLD = "a" * 40, "b" * 40, "c" * 40


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


@pytest.mark.parametrize("build", [STAGING, "abcd1234"], ids=["full-build", "short-build"])
def test_push_to_prod_runs_its_steps_and_asks_for_a_qa_prod_pass(world: World, build: str) -> None:
    probe(world, probes.STAGING, True, build)
    probe(world, probes.PROD, True, PROD)
    world.github.on_run = lambda: probe(world, probes.PROD, True, build)  # the loop sees prod on the new build
    started = github_push.push_prod(world.ctx, {"expected_staging_sha": build}, None)
    ops.join(started["op"], timeout=30)
    op = ops.get(world.ctx, {"id": started["op"]}, None)["op"]
    assert [step["state"] for step in op["steps"]] == [ops.DONE] * 5, op
    # A product may report a short build; its prod workflow checks out the full commit.
    assert world.github.dispatched == [world.github.full_sha(build)]
    assert world.events == [(inbox.QA_PROD_DUE, {"sha": build, "kind": "push"})]
    assert github_push.watching(world.ctx)


def test_a_failed_release_after_a_good_deploy_fails_only_the_last_step(world: World) -> None:
    probe(world, probes.STAGING, True, STAGING)
    probe(world, probes.PROD, True, PROD)
    world.github.on_run = lambda: probe(world, probes.PROD, True, STAGING)
    world.github.releases = "failure"
    started = github_push.push_prod(world.ctx, {"expected_staging_sha": STAGING}, None)
    ops.join(started["op"], timeout=30)
    steps = ops.get(world.ctx, {"id": started["op"]}, None)["op"]["steps"]
    assert [step["state"] for step in steps[:-1]] == [ops.DONE] * 4 and steps[-1]["state"] != ops.DONE
    assert "prod is live" in str(steps[-1])


def test_push_back_goes_only_to_the_build_before_prods_current_one_in_the_deploy_log(world: World) -> None:
    places, at = world.ctx.places, world.clock.iso()
    with pytest.raises(Refused) as refused:
        github_push.push_back(world.ctx, {"expected_sha": OLD}, None)
    assert refused.value.rule == "no-previous"

    for env, commit in ((probes.PROD, OLD), (probes.STAGING, STAGING), (probes.PROD, PROD), (probes.PROD, PROD)):
        deploys.record(places, env, commit, at)
    assert deploys.builds(places, probes.PROD) == {
        "current": {"at": at, "env": probes.PROD, "commit": PROD},
        "previous": {"at": at, "env": probes.PROD, "commit": OLD},
    }
    assert deploys.builds(places, probes.STAGING)["previous"] is None
    with pytest.raises(Refused) as refused:
        github_push.push_back(world.ctx, {"expected_sha": STAGING}, None)
    assert refused.value.rule == "stale"
    assert world.github.dispatched == []
