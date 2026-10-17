from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from loop_fakes import World, commit, git, write

from runtime.queue import formats, records, turn
from runtime.shared import models, store, upgrades
from runtime.shared.models import Probe, Task
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.warden import inbox
from runtime.warden.delivery import deploys, github_push, prod_push
from runtime.warden.health import probes
from runtime.warden.owner import ops, reflections
from runtime.warden.sessions.launch import LaunchError

ROOT = Path(__file__).resolve().parents[2]


def move(src: str, dst: str) -> Callable[[Path], None]:
    def change(root: Path) -> None:
        (root / dst).parent.mkdir(parents=True, exist_ok=True)
        git(root, "mv", src, dst)

    return change


def passed(world: World, tree: str) -> None:
    """The warden's record of a whole check that passed on ``tree``."""
    store.write_json(world.ctx.places.check_passes, [{"tree": tree, "commit": "", "branch": "", "task": "", "at": ""}])


def offer(world: World) -> None:
    """The warden's look for a push to offer, as its pass makes one every watch.branch_retry_minutes."""
    world.clock.t += world.ctx.settings().get_int("watch.branch_retry_minutes") * 60
    github_push.offer(world.ctx)


def not_offered(world: World) -> str:
    offer(world)
    assert github_push.waiting(world.ctx.places) is None
    [entry] = reflections.entries(world.ctx.places)
    return str(entry["text"])


def ship(world: World) -> dict[str, Any]:
    """The warden offers the push, then the owner pushes what waits, as Push to staging does; what waited."""
    offer(world)
    held = github_push.waiting(world.ctx.places)
    assert held is not None
    op = github_push.owner_push(world.ctx, {"sha": held["sha"]}, None)["op"]
    ops.join(op)
    assert ops.get(world.ctx, {"id": op}, None)["op"]["state"] == ops.DONE
    return held


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
    passed(world, git(world.checkout, "rev-parse", f"{sha}^{{tree}}"))
    assert "owner" in not_offered(world)
    assert git(github_repo, "rev-parse", "master") == before


def test_push_sends_only_a_commit_whose_tree_passed_the_checks(world: World, github_repo: Path) -> None:
    checked = commit(world.checkout, write("src/app.py"))
    commit(world.checkout, write("src/later.py"))
    offer(world)
    assert github_push.waiting(world.ctx.places) is None

    passed(world, git(world.checkout, "rev-parse", f"{checked}^{{tree}}"))
    answer = ship(world)
    assert (answer["sha"], answer["commits"]) == (checked, 1)
    assert git(github_repo, "rev-parse", "master") == checked


def test_push_reads_the_agents_checkout_though_another_user_owns_it(
    world: World, github_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked = commit(world.checkout, write("src/app.py"))
    passed(world, git(world.checkout, "rev-parse", f"{checked}^{{tree}}"))
    # Git's own test switch makes every repository another user's; the global config trusts the warden's mirror and
    # GitHub's stand-in, never the agent's checkout.
    config = github_repo.parent / "gitconfig"
    mirror = world.ctx.places.mirror
    config.write_text(config.read_text() + f"[safe]\n\tdirectory = {mirror}\n\tdirectory = {github_repo}\n")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")

    answer = ship(world)

    assert (answer["sha"], answer["commits"]) == (checked, 1)


def test_nothing_reaches_github_until_the_owner_pushes_what_waits(world: World, github_repo: Path) -> None:
    before = git(github_repo, "rev-parse", "master")
    older = commit(world.checkout, write("src/app.py"))
    newer = commit(world.checkout, write("src/later.py"))
    trees = [git(world.checkout, "rev-parse", f"{sha}^{{tree}}") for sha in (older, newer)]
    store.write_json(
        world.ctx.places.check_passes,
        [{"tree": tree, "commit": "", "branch": "", "task": "", "at": ""} for tree in trees],
    )

    offer(world)
    offer(world)  # the waiting push already holds it

    assert git(github_repo, "rev-parse", "master") == before
    held = github_push.waiting(world.ctx.places)
    assert held is not None and (held["sha"], held["commits"]) == (newer, 2)
    with pytest.raises(Refused) as stale:
        github_push.owner_push(world.ctx, {"sha": older}, None)
    assert stale.value.rule == "stale"
    ops.join(github_push.owner_push(world.ctx, {"sha": newer}, None)["op"])
    assert git(github_repo, "rev-parse", "master") == newer and github_push.waiting(world.ctx.places) is None
    assert world.events == [(inbox.PUSHED, {"sha": newer, "commits": 2})]


def test_the_owners_push_checks_again_and_sends_nothing_when_github_moved(world: World, github_repo: Path) -> None:
    checked = commit(world.checkout, write("src/app.py"))
    passed(world, git(world.checkout, "rev-parse", f"{checked}^{{tree}}"))
    offer(world)
    elsewhere = github_repo.parent / "elsewhere"
    git(github_repo.parent, "clone", "-q", str(github_repo), str(elsewhere))
    moved = commit(elsewhere, write("src/other.py"))
    git(elsewhere, "push", "-q", "origin", "master")

    op = github_push.owner_push(world.ctx, {"sha": checked}, None)["op"]
    ops.join(op)

    failed = ops.get(world.ctx, {"id": op}, None)["op"]
    assert failed["state"] == ops.FAILED and "has commits" in failed["steps"][0]["detail"]
    assert git(github_repo, "rev-parse", "master") == moved and github_push.waiting(world.ctx.places) is not None


def probe(world: World, env: str, ok: bool, commit: str = "") -> None:
    """A probe of ``env``; a healthy build is logged as deployed, as the tick logs it."""
    error = "" if ok else "HTTP 502"
    record = Probe(world.clock.iso(), env, ok, commit, 40, error, counts=not ok)
    store.append_jsonl(world.ctx.places.probes, models.dump(record))
    if ok and commit:
        deploys.record(world.ctx.places, env, commit, world.clock.iso())


STAGING, PROD, OLD = "a" * 40, "b" * 40, "c" * 40


def _slot0(world: World, case: str) -> str:
    """Prod on the checkout's tip, then slot 0 on the build after it, cleared to go but for what ``case`` names; that
    build, by the short commit its health answer names."""
    probe(world, probes.PROD, True, git(world.checkout, "rev-parse", "HEAD")[:8])
    if case == "off-the-branch":
        git(world.checkout, "checkout", "-q", "-b", "side")
        build = commit(world.checkout, write("src/side.py"))
        git(world.checkout, "checkout", "-q", "master")
    else:
        build = _merge(world, "impl-t-0001")
        _task(world, "t-0001", "merged" if case == "merge-not-checked" else "checked", build)
    if case != "tree-never-checked":
        passed(world, git(world.checkout, "rev-parse", f"{build}^{{tree}}"))
    staging = {"never-probed": None, "red": False}.get(case, True)
    if staging is not None:
        probe(world, probes.STAGING, staging, build[:8] if staging else "")
    if case == "prod-runs-it":
        probe(world, probes.PROD, True, build[:8])
    return build[:8]


@pytest.mark.parametrize(
    ("case", "rule", "said"),
    [
        ("stale", "stale", "look again"),
        ("never-probed", "staging-unknown", "staging has not been probed yet"),
        ("red", "staging-red", "staging is red (HTTP 502)"),
        ("prod-runs-it", "nothing-to-push", "prod already runs"),
        ("off-the-branch", "off-branch", "is not on master in the checkout"),
        ("tree-never-checked", "unchecked", "has not passed factory check"),
        ("merge-not-checked", "uncleared", "(t-0001, merged), whose check of the default branch has not passed"),
    ],
)
def test_push_to_prod_refuses_saying_why(world: World, github_repo: Path, case: str, rule: str, said: str) -> None:
    build = _slot0(world, case)

    with pytest.raises(Refused) as refused:
        prod_push.push_prod(world.ctx, {"expected_staging_sha": "f" * 8 if case == "stale" else build}, None)

    assert (refused.value.rule, said in refused.value.reason) == (rule, True), refused.value.reason
    assert world.launcher.deployed == []


@pytest.mark.parametrize("zero_origin", ["", "http://203.0.113.5:9101"], ids=["slot-0-on-loopback", "slot-0-public"])
def test_push_to_prod_deploys_slot_0s_build_in_full_and_asks_for_a_qa_prod_pass(
    world: World, github_repo: Path, zero_origin: str
) -> None:
    changed = world.ctx.settings().with_changes({"slots.zero_origin": zero_origin})
    world.ctx.settings = lambda: changed  # a tester's browser opens slot 0 where its product says it is
    build = _slot0(world, "cleared")
    world.launcher.deploy = lambda sha: (probe(world, probes.PROD, True, build), (0, ""))[1]  # prod answers on it

    started = prod_push.push_prod(world.ctx, {"expected_staging_sha": build}, None)
    ops.join(started["op"], timeout=30)

    op = ops.get(world.ctx, {"id": started["op"]}, None)["op"]
    assert [step["state"] for step in op["steps"]] == [ops.DONE] * 3, op
    assert world.launcher.deployed == [(git(world.checkout, "rev-parse", "master"), False)]
    slot0 = zero_origin or f"http://127.0.0.1:{leases.port(world.ctx.settings(), 0)}"
    where = {"prod": "https://notes.example.com", "staging": slot0, "health": "/healthz"}
    assert world.events == [(inbox.QA_PROD_DUE, {"sha": build, "kind": "push", "since": world.clock.iso(), **where})]
    assert prod_push.watching(world.ctx)


def _launch_failed(sha: str) -> tuple[int, str]:
    raise LaunchError("software-deploy-run@d1 left no result (FileNotFoundError)")


@pytest.mark.parametrize(
    ("deploy", "said"),
    [
        (lambda sha: (1, "building web\nfailed: the build of web exited 1\n"), "the build of web exited 1"),
        (lambda sha: (1, "failed: factory.toml:\n  [deploy].health: missing\n"),
         "factory.toml: [deploy].health: missing"),
        (lambda sha: (2, "refused\n"), "the build and deploy exited 2, and its log names no failure"),
        (_launch_failed, "software-deploy-run@d1 left no result (FileNotFoundError)"),
    ],
    ids=["failed-line", "problems-under-it", "no-failed-line", "unit-left-nothing"],
)  # fmt: skip
def test_a_failed_build_or_deploy_says_what_its_log_says_failed_and_is_logged(
    world: World, github_repo: Path, caplog: pytest.LogCaptureFixture, deploy: Callable[[str], tuple[int, str]],
    said: str
) -> None:  # fmt: skip
    build = _slot0(world, "cleared")
    world.launcher.deploy = deploy

    with caplog.at_level(logging.WARNING):
        started = prod_push.push_prod(world.ctx, {"expected_staging_sha": build}, None)
        ops.join(started["op"], timeout=30)

    steps = ops.get(world.ctx, {"id": started["op"]}, None)["op"]["steps"]
    assert [(step["state"], step["detail"]) for step in steps[:2]] == [(ops.FAILED, said), (ops.WAITING, "")]
    assert [r.getMessage() for r in caplog.records] == [f"warden: operation {started['op']} step 0 failed (StepError)"]


@pytest.mark.parametrize("on_branch", [True, False], ids=["on-the-branch", "off-it"])
def test_push_back_goes_while_staging_is_red_to_a_build_never_checked_but_only_from_the_branch(
    world: World, github_repo: Path, on_branch: bool
) -> None:
    if not on_branch:
        git(world.checkout, "checkout", "-q", "-b", "side")
    old = commit(world.checkout, write("src/old.py"))[:8]
    git(world.checkout, "checkout", "-q", "master")
    for build in (old, commit(world.checkout, write("src/new.py"))[:8]):
        probe(world, probes.PROD, True, build)
    probe(world, probes.STAGING, False)
    world.launcher.deploy = lambda sha: (probe(world, probes.PROD, True, old), (0, ""))[1]

    if not on_branch:
        with pytest.raises(Refused) as refused:
            prod_push.push_back(world.ctx, {"expected_sha": old}, None)
        assert refused.value.rule == "off-branch" and world.launcher.deployed == []
        return
    ops.join(prod_push.push_back(world.ctx, {"expected_sha": old}, None)["op"], timeout=30)
    assert world.launcher.deployed == [(git(world.checkout, "rev-parse", "master^"), True)]


def test_push_back_goes_only_to_the_build_before_prods_current_one_in_the_deploy_log(world: World) -> None:
    places, at = world.ctx.places, world.clock.iso()
    with pytest.raises(Refused) as refused:
        prod_push.push_back(world.ctx, {"expected_sha": OLD}, None)
    assert refused.value.rule == "no-previous"

    for env, build in ((probes.PROD, OLD), (probes.STAGING, STAGING), (probes.PROD, PROD), (probes.PROD, PROD)):
        deploys.record(places, env, build, at)
    assert deploys.builds(places, probes.PROD) == {
        "current": {"at": at, "env": probes.PROD, "commit": PROD},
        "previous": {"at": at, "env": probes.PROD, "commit": OLD},
    }
    assert deploys.builds(places, probes.STAGING)["previous"] is None
    with pytest.raises(Refused) as refused:
        prod_push.push_back(world.ctx, {"expected_sha": STAGING}, None)
    assert refused.value.rule == "stale"
    assert world.launcher.deployed == []


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
        assert ship(world)["sha"] == merge  # the newest shippable commit
    else:
        offer(world)
        assert github_push.waiting(world.ctx.places) is None


def test_a_merge_reverted_in_the_range_ships_nothing_of_its_own(world: World, github_repo: Path) -> None:
    merge = _merge(world, "impl-t-0001")
    _task(world, "t-0001", "building", merge)
    git(world.checkout, "revert", "-m", "1", "--no-edit", merge)
    head = git(world.checkout, "rev-parse", "HEAD")
    passed(world, git(world.checkout, "rev-parse", f"{head}^{{tree}}"))

    assert ship(world)["sha"] == head


def test_push_back_goes_to_what_prod_ran_when_the_last_push_began_even_if_that_push_never_answered(
    world: World, github_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploys.record(world.ctx.places, probes.PROD, OLD, world.clock.iso())
    build = _slot0(world, "cleared")
    prod = deploys.builds(world.ctx.places, probes.PROD)["current"]
    monkeypatch.setattr(prod_push, "_steps", lambda push: [])  # the push starts, and its build never answers

    prod_push.push_prod(world.ctx, {"expected_staging_sha": build}, None)

    assert prod is not None and prod_push.push_back_target(world.ctx) == prod["commit"]  # not OLD, two builds back


def _pin(world: World, ref: str = "acme/software-factory@" + "d" * 40) -> str:
    """The commit an upgrade makes, as root, recorded as the owner's with each path's blob."""
    parent = git(world.checkout, "rev-parse", "HEAD")

    def change(root: Path) -> None:
        (root / ".github").mkdir(exist_ok=True)
        (root / ".github" / "factory-ref").write_text(ref + "\n")
        write(".github/workflows/ci.yml")(root)

    made = commit(world.checkout, change)
    places = world.ctx.places
    places.upgrade.mkdir(parents=True, exist_ok=True)
    blobs = {
        ".github/factory-ref": upgrades.blob_id(ref + "\n"),
        ".github/workflows/ci.yml": upgrades.blob_id("changed\n"),
    }
    store.write_json(upgrades.pins_path(places),
                     [*upgrades.read_pins(places), {"commit": made, "parent": parent, "blobs": blobs}])  # fmt: skip
    return made


@pytest.mark.parametrize(
    ("after", "ships"),
    [(None, True), ("src/app.py", True), (".github/factory-ref", False)],
    ids=["the-pin-alone", "a-checked-commit-on-top", "the-pin's-file-changed-again"],
)
def test_the_owners_pin_ships_on_its_parents_check_and_lets_nothing_else_through(
    world: World, github_repo: Path, after: str | None, ships: bool
) -> None:
    passed(world, git(world.checkout, "rev-parse", "HEAD^{tree}"))
    pin = _pin(world)
    head = commit(world.checkout, write(after)) if after else pin
    if after:
        passed(world, git(world.checkout, "rev-parse", f"{head}^{{tree}}"))

    if ships:
        assert ship(world)["sha"] == head
        assert git(github_repo, "rev-parse", "master") == head
    else:
        assert "which only the owner changes" in not_offered(world)


def test_a_change_to_the_pins_files_that_root_did_not_make_is_refused(world: World, github_repo: Path) -> None:
    passed(world, git(world.checkout, "rev-parse", "HEAD^{tree}"))
    sha = commit(world.checkout, write(".github/factory-ref"))  # an agent's, not in root's record
    passed(world, git(world.checkout, "rev-parse", f"{sha}^{{tree}}"))
    assert "which only the owner changes" in not_offered(world)


def test_the_warden_offers_a_pins_push_and_looks_again_every_few_minutes_or_once_a_task_is_checked(
    world: World, github_repo: Path
) -> None:
    before = git(github_repo, "rev-parse", "master")
    passed(world, git(world.checkout, "rev-parse", "HEAD^{tree}"))
    pin = _pin(world)

    offer(world)

    assert git(github_repo, "rev-parse", "master") == before
    held = github_push.waiting(world.ctx.places)
    assert held is not None and held["sha"] == pin and held["items"] == [{"kind": "factory", "task": ""}]
    ops.join(github_push.owner_push(world.ctx, {"sha": pin}, None)["op"])
    later = commit(world.checkout, write("src/app.py"))
    passed(world, git(world.checkout, "rev-parse", f"{later}^{{tree}}"))
    github_push.offer(world.ctx)  # within watch.branch_retry_minutes of the last look: no fetch
    assert github_push.waiting(world.ctx.places) is None
    github_push.look_soon(world.ctx.places)
    github_push.offer(world.ctx)
    held = github_push.waiting(world.ctx.places)
    assert held is not None and held["sha"] == later


def test_a_pin_on_a_head_not_yet_checked_waits_quietly(
    world: World, github_repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _pin(world)  # its parent never passed factory check

    with caplog.at_level(logging.WARNING):
        offer(world)

    # The routine wait was logged as an error with its trace, 21 times in one evening.
    assert caplog.records == [] and github_push.waiting(world.ctx.places) is None


def test_a_git_failure_names_git_s_own_reason_not_its_closing_advice(
    world: World, github_repo: Path, tmp_path: Path
) -> None:
    with pytest.raises(Refused, match="does not appear to be a git repository"):
        github_push.Git(world.ctx).run("fetch", "--quiet", str(tmp_path / "nowhere"), "+refs/heads/x:refs/x")


def test_an_upgrade_s_pin_on_the_last_one_not_yet_pushed_goes_out_after_it(
    world: World, github_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    passed(world, git(world.checkout, "rev-parse", "HEAD^{tree}"))
    first = _pin(world, "acme/software-factory@" + "d" * 40)
    second = _pin(world, "acme/software-factory@" + "e" * 40)

    offer(world)
    held = github_push.waiting(world.ctx.places)
    assert held is not None and held["sha"] == second
    ops.join(github_push.owner_push(world.ctx, {"sha": second}, None)["op"])

    assert git(github_repo, "rev-parse", "master") == second
    assert git(github_repo, "rev-parse", "master^") == first


def _owned(world: World) -> str:
    """The owner's own change to factory.toml, as root commits and records it (factory-own)."""
    parent = git(world.checkout, "rev-parse", "HEAD")
    text = (world.checkout / "factory.toml").read_text() + "# the owner's own line\n"
    made = commit(world.checkout, lambda root: (root / "factory.toml").write_text(text))
    places = world.ctx.places
    places.upgrade.mkdir(parents=True, exist_ok=True)
    record = {"commit": made, "parent": parent, "blobs": {"factory.toml": upgrades.blob_id(text)}}
    store.write_json(upgrades.owned_path(places), [*upgrades.read_owned(places), record])
    return made


def test_the_owners_own_change_waits_for_a_check_it_asks_for_once_then_ships_as_theirs(
    world: World, github_repo: Path
) -> None:
    passed(world, git(world.checkout, "rev-parse", "HEAD^{tree}"))
    owned = _owned(world)

    offer(world)
    github_push.look_soon(world.ctx.places)
    offer(world)

    assert github_push.waiting(world.ctx.places) is None and reflections.entries(world.ctx.places) == []
    assert [e for e in world.events if e[0] == inbox.CHECK_DUE] == [(inbox.CHECK_DUE, {"commit": owned})]
    passed(world, git(world.checkout, "rev-parse", f"{owned}^{{tree}}"))
    held = ship(world)
    assert held["sha"] == owned and held["items"] == [{"kind": "owner", "task": ""}]
    assert git(github_repo, "rev-parse", "master") == owned


@pytest.mark.parametrize("why", ["a-task-holds-the-turn", "an-unchecked-merge", "nothing-new"])
def test_no_check_is_asked_for_while_the_tip_waits_on_more_than_factory_check(
    world: World, github_repo: Path, monkeypatch: pytest.MonkeyPatch, why: str
) -> None:
    passed(world, git(world.checkout, "rev-parse", "HEAD^{tree}"))
    if why == "a-task-holds-the-turn":
        commit(world.checkout, write("src/app.py"))
        monkeypatch.setattr(turn, "holder", lambda places: {"task": "t-0001", "branch": "b", "base": "c"})
    elif why == "an-unchecked-merge":
        git(world.checkout, "checkout", "-q", "-b", "side")
        commit(world.checkout, write("src/side.py"))
        git(world.checkout, "checkout", "-q", "-")
        git(world.checkout, "merge", "-q", "--no-ff", "-m", "Merge side", "side")

    offer(world)

    assert [e for e in world.events if e[0] == inbox.CHECK_DUE] == []
