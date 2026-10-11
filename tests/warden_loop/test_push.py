from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from loop_fakes import World

from runtime.queue import formats, records, turn
from runtime.shared import models, store
from runtime.shared.models import Probe, Task
from runtime.shared.protocol import Refused
from runtime.warden import inbox
from runtime.warden.delivery import deploys, github_push, prod_push
from runtime.warden.health import probes
from runtime.warden.owner import ops

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


def passed(world: World, tree: str) -> None:
    """The warden's record of a whole check that passed on ``tree``."""
    store.write_json(world.ctx.places.check_passes, [{"tree": tree, "commit": "", "branch": "", "task": "", "at": ""}])


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


def test_the_warden_trusts_github_by_the_host_keys_it_pinned_and_no_other(world: World, github_repo: Path) -> None:
    command = github_push.Git(world.ctx).env["GIT_SSH_COMMAND"]
    assert "-o StrictHostKeyChecking=yes" in command
    assert f"-o UserKnownHostsFile={world.ctx.places.github_known_hosts}" in command


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

    passed(world, git(world.checkout, "rev-parse", f"{checked}^{{tree}}"))
    answer = github_push.push_checked(world.ctx, {"sha": checked}, None)
    assert answer["commits"] == 1
    assert git(github_repo, "rev-parse", "master") == checked


def test_push_reads_the_agents_checkout_though_another_user_owns_it(
    world: World, github_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked = commit(world.checkout, write("src/app.py"))
    passed(world, git(world.checkout, "rev-parse", f"{checked}^{{tree}}"))
    # Git's own test switch makes every repository another user's; the global config trusts the warden's mirror and
    # GitHub's stand-in, never the agent's checkout.
    config = github_repo.parent / "gitconfig"
    mirror = github_push.mirror_path(world.ctx.places)
    config.write_text(config.read_text() + f"[safe]\n\tdirectory = {mirror}\n\tdirectory = {github_repo}\n")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

    answer = github_push.push_checked(world.ctx, {"sha": checked}, None)

    assert answer["commits"] == 1


def probe(world: World, env: str, ok: bool, commit: str = "", on_default: bool | None = True) -> None:
    """A probe of ``env``; a healthy staging build is noted as deployed, and whether it is on the default branch, as
    the tick notes it."""
    error = "" if ok else "HTTP 502"
    record = Probe(world.clock.iso(), env, ok, commit, 40, error, counts=not ok)
    store.append_jsonl(world.ctx.places.probes, models.dump(record))
    if env == probes.STAGING and ok and commit:
        deploys.record(world.ctx.places, env, commit, world.clock.iso())
        deploys.note_branch(world.ctx.places, env, commit, on_default, world.clock.now())


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
        prod_push.push_prod(world.ctx, {"expected_staging_sha": expected}, None)
    assert refused.value.rule == rule
    assert world.github.dispatched == []


@pytest.mark.parametrize("build", [STAGING, "abcd1234"], ids=["full-build", "short-build"])
def test_push_to_prod_runs_its_steps_and_asks_for_a_qa_prod_pass(world: World, build: str) -> None:
    probe(world, probes.STAGING, True, build)
    probe(world, probes.PROD, True, PROD)
    world.github.on_run = lambda: probe(world, probes.PROD, True, build)  # the loop sees prod on the new build
    started = prod_push.push_prod(world.ctx, {"expected_staging_sha": build}, None)
    ops.join(started["op"], timeout=30)
    op = ops.get(world.ctx, {"id": started["op"]}, None)["op"]
    assert [step["state"] for step in op["steps"]] == [ops.DONE] * 5, op
    # A product may report a short build; its prod workflow checks out the full commit.
    assert world.github.dispatched == [world.github.full_sha(build)]
    assert world.events == [(inbox.QA_PROD_DUE, {"sha": build, "kind": "push", "since": world.clock.iso()})]
    assert prod_push.watching(world.ctx)


def test_a_failed_release_after_a_good_deploy_fails_only_the_last_step(world: World) -> None:
    probe(world, probes.STAGING, True, STAGING)
    probe(world, probes.PROD, True, PROD)
    world.github.on_run = lambda: probe(world, probes.PROD, True, STAGING)
    world.github.releases = "failure"
    started = prod_push.push_prod(world.ctx, {"expected_staging_sha": STAGING}, None)
    ops.join(started["op"], timeout=30)
    steps = ops.get(world.ctx, {"id": started["op"]}, None)["op"]["steps"]
    assert [step["state"] for step in steps[:-1]] == [ops.DONE] * 4 and steps[-1]["state"] != ops.DONE
    assert "prod is live" in str(steps[-1])
    assert prod_push.released(world.ctx.places) is None  # the next push tries the release again


def test_the_next_push_releases_what_changed_since_the_last_releases_that_finished(world: World) -> None:
    later = "d" * 40
    probe(world, probes.PROD, True, PROD)
    for build in (STAGING, later):
        probe(world, probes.STAGING, True, build)
        world.github.on_run = lambda build=build: probe(world, probes.PROD, True, build)
        started = prod_push.push_prod(world.ctx, {"expected_staging_sha": build}, None)
        ops.join(started["op"], timeout=30)

    assert world.github.released == ["-", STAGING]


def test_push_back_goes_only_to_the_build_before_prods_current_one_in_the_deploy_log(world: World) -> None:
    places, at = world.ctx.places, world.clock.iso()
    with pytest.raises(Refused) as refused:
        prod_push.push_back(world.ctx, {"expected_sha": OLD}, None)
    assert refused.value.rule == "no-previous"

    for env, commit in ((probes.PROD, OLD), (probes.STAGING, STAGING), (probes.PROD, PROD), (probes.PROD, PROD)):
        deploys.record(places, env, commit, at)
    assert deploys.builds(places, probes.PROD) == {
        "current": {"at": at, "env": probes.PROD, "commit": PROD},
        "previous": {"at": at, "env": probes.PROD, "commit": OLD},
    }
    assert deploys.builds(places, probes.STAGING)["previous"] is None
    with pytest.raises(Refused) as refused:
        prod_push.push_back(world.ctx, {"expected_sha": STAGING}, None)
    assert refused.value.rule == "stale"
    assert world.github.dispatched == []


def _merge(world: World, name: str) -> str:
    """A branch with one change, merged into master with --no-ff; the merge commit."""
    git(world.checkout, "checkout", "-q", "-b", name)
    commit(world.checkout, write(f"src/{name}.py"))
    git(world.checkout, "checkout", "-q", "master")
    git(world.checkout, "merge", "-q", "--no-ff", name, "-m", f"Merge {name}")
    return git(world.checkout, "rev-parse", "HEAD")


def _task(world: World, task_id: str, status: str, merge: str) -> None:
    queue = world.checkout / "queue" / "tasks"
    for folder in (queue.parent, queue):
        folder.mkdir(exist_ok=True)
        folder.chmod(0o755)
    task = Task(id=task_id, title="A change", requests=["r-2026-10-09-0001"], goal_item="G1", severity="major",
                surfaces=["api"], status=status, created="2026-10-09T00:00:00Z", body="Change it.", run=1,
                slices=1)  # fmt: skip
    (queue / f"{task_id}.md").write_text(formats.dump_task(task))
    known = turn.merges(world.ctx.places)
    store.write_json(turn.merges_path(world.ctx.places), {**known, merge: task_id})


@pytest.mark.parametrize(
    ("status", "check", "ships"),
    [("merged", "", False), ("checked", "", True), ("closed", "", False), ("closed", "fail", False),
     ("closed", "pass", True)],
    ids=["merged", "checked", "closed-before-its-check", "closed-after-a-failed-check", "closed-after-its-check"],
)  # fmt: skip
def test_push_never_ships_a_merge_whose_check_of_the_default_branch_has_not_passed(
    world: World, github_repo: Path, status: str, check: str, ships: bool
) -> None:
    merge = _merge(world, "impl-t-0001")
    _task(world, "t-0001", status, merge)
    passed(world, git(world.checkout, "rev-parse", f"{merge}^{{tree}}"))
    if check:
        row = records.ScenarioRow("sign-in", check, 3, "shot.png")
        saved = records.CheckRecord(f"c-20261009T000000Z-{merge[:7]}", merge, "2026-10-09T00:00:00Z", "Check", [row])
        (world.checkout / "queue" / "checks").mkdir(mode=0o755)
        (world.checkout / "queue" / "checks" / f"{saved.id}.md").write_text(records.dump_check(saved))

    if ships:
        assert github_push.push_checked(world.ctx, {}, None)["sha"] == merge  # the newest shippable commit
    else:
        with pytest.raises(Refused, match=f"t-0001, {status}") as refused:
            github_push.push_checked(world.ctx, {}, None)
        assert refused.value.rule == "unchecked"


def test_a_merge_reverted_in_the_range_ships_nothing_of_its_own(world: World, github_repo: Path) -> None:
    merge = _merge(world, "impl-t-0001")
    _task(world, "t-0001", "building", merge)
    git(world.checkout, "revert", "-m", "1", "--no-edit", merge)
    head = git(world.checkout, "rev-parse", "HEAD")
    passed(world, git(world.checkout, "rev-parse", f"{head}^{{tree}}"))

    assert github_push.push_checked(world.ctx, {}, None)["sha"] == head


def test_push_back_goes_to_what_prod_ran_when_the_last_push_began_even_if_that_push_never_answered(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    for commit in (OLD, PROD):
        deploys.record(world.ctx.places, probes.PROD, commit, world.clock.iso())
    probe(world, probes.STAGING, True, STAGING)
    probe(world, probes.PROD, True, PROD)
    monkeypatch.setattr(prod_push, "_steps", lambda *args: [])  # the push starts, and its build never answers

    prod_push.push_prod(world.ctx, {"expected_staging_sha": STAGING}, None)

    assert prod_push.push_back_target(world.ctx) == PROD  # not OLD, two builds back


@pytest.mark.parametrize("on_default", [False, None], ids=["off-it", "unknown"])
def test_a_staging_build_not_known_on_the_default_branch_never_goes_to_prod(
    world: World, on_default: bool | None
) -> None:
    probe(world, probes.STAGING, True, STAGING, on_default)
    probe(world, probes.PROD, True, PROD)

    with pytest.raises(Refused) as refused:
        prod_push.push_prod(world.ctx, {"expected_staging_sha": STAGING}, None)
    assert refused.value.rule == "staging-pinned"


def test_a_build_github_did_not_place_on_the_branch_is_asked_again_and_reported_once(
    world: World, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    deploys.record(world.ctx.places, probes.STAGING, STAGING, world.clock.iso())
    answers: list[bool | None] = [None, None, True]  # None: GitHub did not answer

    def on_default(ctx: object, commit: str) -> bool:
        answer = answers.pop(0)
        if answer is None:
            raise Refused("push-failed", "git fetch failed: Connection timed out")
        return answer

    monkeypatch.setattr(prod_push, "on_default", on_default)
    retry = world.ctx.settings().get_int("watch.branch_retry_minutes") * 60
    with caplog.at_level(logging.WARNING, logger=github_push.__name__):
        for wait, left in ((0, 2), (retry - 1, 2), (1, 1), (retry, 0), (retry, 0)):  # the same build all along
            world.clock.t += wait
            prod_push.ask_branch(world.ctx)
            assert len(answers) == left  # asked again only once the retry is due, and never once known
    assert deploys.on_default(world.ctx.places, probes.STAGING) is True
    assert [r.getMessage() for r in caplog.records] == [
        f"warden: whether staging's build {STAGING[:12]} is on the default branch is unknown "
        "(git fetch failed: Connection timed out)"
    ]


def test_whether_a_commit_is_on_the_default_branch_is_asked_of_github_itself(world: World, github_repo: Path) -> None:
    pushed = git(world.checkout, "rev-parse", "HEAD")
    commit(world.checkout, write("src/local.py"))
    local = git(world.checkout, "rev-parse", "HEAD")

    assert prod_push.on_default(world.ctx, pushed) is True
    assert prod_push.on_default(world.ctx, local) is False  # GitHub's branch does not hold it
    shutil.rmtree(github_repo)  # GitHub does not answer
    with pytest.raises(Refused, match="git fetch failed"):
        prod_push.on_default(world.ctx, pushed)
