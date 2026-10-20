from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from loop_fakes import World, commit, git, passed, write

from runtime.queue import formats, turn
from runtime.shared import models, store
from runtime.shared.models import Probe, Task
from runtime.shared.protocol import Refused
from runtime.slots import leases
from runtime.warden import inbox
from runtime.warden.delivery import deploys, github_push, prod_push, push_plan
from runtime.warden.health import probes
from runtime.warden.owner import ops
from runtime.warden.sessions.launch import LaunchError

ROOT = Path(__file__).resolve().parents[2]


def test_the_warden_trusts_github_by_the_host_keys_it_pinned_and_no_other(world: World, github_repo: Path) -> None:
    command = github_push.Git(world.ctx).env["GIT_SSH_COMMAND"]
    assert "-o StrictHostKeyChecking=yes" in command
    assert f"-o UserKnownHostsFile={world.ctx.places.github_known_hosts}" in command


def probe(world: World, env: str, ok: bool, commit: str = "") -> None:
    """A probe of ``env``; a healthy build is logged as deployed, as the tick logs it."""
    error = "" if ok else "HTTP 502"
    record = Probe(world.clock.iso(), env, ok, commit, 40, error, counts=not ok)
    store.append_jsonl(world.ctx.places.probes, models.dump(record))
    if ok and commit:
        deploys.record(world.ctx.places, env, commit, world.clock.iso())


STAGING, PROD, OLD = "a" * 40, "b" * 40, "c" * 40


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
        ("tree-never-checked", "unchecked", "t-0001 is held back (factory check has not passed on it yet)"),
        ("merge-not-checked", "uncleared", "t-0001 is held back (its check has not passed)"),
    ],
)
def test_push_to_prod_refuses_saying_why(world: World, github_repo: Path, case: str, rule: str, said: str) -> None:
    build = _slot0(world, case)

    with pytest.raises(Refused) as refused:
        prod_push.push_prod(world.ctx, {"ships_sha": "f" * 8 if case == "stale" else build}, None)

    assert (refused.value.rule, said in refused.value.reason) == (rule, True), refused.value.reason
    assert world.launcher.deployed == []


def _steps(world: World, steps: list[str]) -> dict[str, str]:
    """Each step's commit on master, by name: `t-0001 checked` merges t-0001 at that status, `revert-t-0001` reverts
    it. `checked` marks a tree that passed factory check, `served` a build staging ran, `green` both."""
    made: dict[str, str] = {}
    for n, step in enumerate(steps):
        name, *marks = step.split()
        if name.startswith("revert-"):
            git(world.checkout, "revert", "-m", "1", "--no-edit", made[name.removeprefix("revert-")])
            made[name] = git(world.checkout, "rev-parse", "HEAD")
        else:
            made[name] = _merge(world, f"impl-{name}-{n}")
            _task(world, name, marks.pop(0), made[name])
        if {"green", "checked"} & set(marks):
            passed(world, git(world.checkout, "rev-parse", f"{made[name]}^{{tree}}"))
        if {"green", "served"} & set(marks):
            probe(world, probes.STAGING, True, made[name][:8])
    return made


def _history(world: World, steps: list[str]) -> dict[str, str]:
    (world.checkout / ".git" / "info").mkdir(exist_ok=True)
    (world.checkout / ".git" / "info" / "exclude").write_text("queue/\n")  # a revert leaves the task files be
    probe(world, probes.PROD, True, git(world.checkout, "rev-parse", "HEAD")[:8])
    world.launcher.deploy = lambda sha: (probe(world, probes.PROD, True, sha), (0, ""))[1]
    return _steps(world, steps)


HELD = "its check has not passed"


@pytest.mark.parametrize(
    ("steps", "ships", "gains", "held"),
    [
        (
            ["t-0001 checked green", "t-0002 merged green", "t-0003 checked green"],
            "t-0001",
            ["t-0001"],
            [("t-0002", HELD), ("t-0003", "comes after t-0002, which is held")],
        ),
        (
            ["t-0001 checked green", "t-0002 merged", "revert-t-0002 green", "t-0002 merged", "t-0003 merged green"],
            "revert-t-0002",
            ["t-0001"],
            [("t-0002", HELD), ("t-0003", HELD)],
        ),
        (
            ["t-0001 checked green", "t-0002 checked checked", "t-0003 checked served"],
            "t-0001",
            ["t-0001"],
            [("t-0002", "staging has not run it yet"), ("t-0003", "factory check has not passed on it yet")],
        ),
        (
            ["t-0001 merged green", "t-0002 checked green"],
            None,
            [],
            [("t-0001", HELD), ("t-0002", "comes after t-0001, which is held")],
        ),
    ],
    ids=["newest-green", "a-revert-clears-its-merge", "a-build-staging-never-ran", "nothing-green"],
)
def test_the_push_ships_the_newest_green_build_and_holds_back_each_merge_after_it_saying_why(
    world: World,
    github_repo: Path,
    steps: list[str],
    ships: str | None,
    gains: list[str],
    held: list[tuple[str, str]],
) -> None:
    made = _history(world, steps)

    if ships is None:
        with pytest.raises(Refused) as refused:
            prod_push.push_prod(world.ctx, {"ships_sha": "f" * 8}, None)
        assert refused.value.reason.endswith(f"{held[0][0]} is held back ({held[0][1]})"), refused.value.reason
    else:
        ops.join(prod_push.push_prod(world.ctx, {"ships_sha": made[ships][:8]}, None)["op"], timeout=30)
        assert world.launcher.deployed == [(made[ships], False)]
    plan = push_plan.read(world.ctx.places)
    assert plan is not None and plan["ships"] == (None if ships is None else {**plan["ships"], "full": made[ships]})
    assert (plan["gains"], [(row["task"], row["why"]) for row in plan["held"]]) == (gains, held)


def test_a_press_for_a_build_the_plan_no_longer_ships_is_refused_and_the_plan_then_names_the_new_one(
    world: World, github_repo: Path
) -> None:
    shown = _history(world, ["t-0001 checked green"])["t-0001"]
    push_plan.refresh(world.ctx)
    moved = _steps(world, ["t-0002 checked green"])["t-0002"]

    with pytest.raises(Refused) as refused:
        prod_push.push_prod(world.ctx, {"ships_sha": shown}, None)

    plan = push_plan.read(world.ctx.places)
    assert refused.value.rule == "stale" and plan is not None and plan["ships"]["full"] == moved
    assert world.launcher.deployed == []


@pytest.mark.parametrize("zero_origin", ["", "http://203.0.113.5:9101"], ids=["slot-0-on-loopback", "slot-0-public"])
def test_push_to_prod_deploys_slot_0s_build_in_full_and_asks_for_a_qa_prod_pass(
    world: World, github_repo: Path, zero_origin: str
) -> None:
    changed = world.ctx.settings().with_changes({"slots.zero_origin": zero_origin})
    world.ctx.settings = lambda: changed  # a tester's browser opens slot 0 where its product says it is
    build = _slot0(world, "cleared")
    world.launcher.deploy = lambda sha: (probe(world, probes.PROD, True, build), (0, ""))[1]  # prod answers on it

    started = prod_push.push_prod(world.ctx, {"ships_sha": build}, None)
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
        started = prod_push.push_prod(world.ctx, {"ships_sha": build}, None)
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


def test_push_back_goes_to_what_prod_ran_when_the_last_push_began_even_if_that_push_never_answered(
    world: World, github_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deploys.record(world.ctx.places, probes.PROD, OLD, world.clock.iso())
    build = _slot0(world, "cleared")
    prod = deploys.builds(world.ctx.places, probes.PROD)["current"]
    monkeypatch.setattr(prod_push, "_steps", lambda push: [])  # the push starts, and its build never answers

    prod_push.push_prod(world.ctx, {"ships_sha": build}, None)

    assert prod is not None and prod_push.push_back_target(world.ctx) == prod["commit"]  # not OLD, two builds back


def test_a_git_failure_names_git_s_own_reason_not_its_closing_advice(
    world: World, github_repo: Path, tmp_path: Path
) -> None:
    with pytest.raises(Refused, match="does not appear to be a git repository"):
        github_push.Git(world.ctx).run("fetch", "--quiet", str(tmp_path / "nowhere"), "+refs/heads/x:refs/x")
