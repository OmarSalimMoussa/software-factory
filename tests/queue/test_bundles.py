"""Merge-check bundles: the merges a check of the default branch clears together, and what a failed row asks next."""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any

import pytest
from product_git import ProductGit, run
from queue_kit import World

from runtime.qa import qa_browser, walked
from runtime.qa.scenarios import FOLDER
from runtime.queue import bundle, records, turn, verbs
from runtime.queue.book import Queue
from runtime.shared import models, store
from runtime.shared.models import Probe
from runtime.shared.protocol import Refused
from runtime.slots import health, leases
from runtime.warden.context import StackResult
from runtime.warden.delivery import deploys
from runtime.warden.sessions import backstop

SCENARIOS = {
    "web": "protects: web/\nneeds: nothing\nruns: in-turn",
    "api": "protects: api/handlers.py\nneeds: nothing\nruns: in-turn",
    "later": "protects: web/\nneeds: seed none-yet\nruns: waiting",
}


class Product(ProductGit):
    """The world's checkout as a git repo: merges recorded for tasks, and the checks the orchestrator saves of it."""

    def __init__(self, world: World) -> None:
        super().__init__(world.repo, SCENARIOS)
        self.world = world
        world.launcher.git = run

    def merge(self, rel: str, status: str = "merged") -> tuple[str, str]:
        """A merge recorded as its task's, at ``status``: the merge and the task."""
        commit, branch = super().merge(rel)
        task = self.world.task(branch=branch)
        self.world.queue().write_task(dataclasses.replace(task, status=status, run=1))
        return self.record(commit, task.id), task.id

    def record(self, commit: str, task: str) -> str:
        found = turn.merges(self.world.places)
        store.write_json(turn.merges_path(self.world.places), {**found, commit: task})
        return commit

    def slot0(self, commit: str) -> None:
        """Slot 0's probe, naming the build it runs as its health answer does: by the short commit."""
        probe = Probe(self.world.clock.iso(), "staging", True, commit[:8], 40)
        store.append_jsonl(self.world.places.probes, models.dump(probe))

    def check(self, commit: str, **rows: str) -> dict[str, Any]:
        table = "".join(f"| {name} | {result} | 1 | shot.png |\n" for name, result in rows.items())
        report = f"# Check\n\n## Scenarios\n\n{records.CHECK_TABLE}\n| --- | --- | --- | --- |\n{table}"
        self.world.clock.at += 60
        return verbs.check_save(self.world.ctx(), {"commit": commit}, report.encode())

    def status(self, task: str) -> str:
        return self.world.queue().task(task).status


@pytest.fixture
def product(world: World, monkeypatch: pytest.MonkeyPatch) -> Product:
    monkeypatch.setattr(qa_browser, "runs_root", lambda: world.repo.parent / "runs")
    return Product(world)


@pytest.mark.parametrize(("cap", "count"), [(2, 2), (3, 3)])
def test_a_bundle_is_the_oldest_waiting_merges_up_to_the_cap_walked_before_the_next_merge(
    world: World, product: Product, cap: int, count: int
) -> None:
    product.merge("web/old.js", status="checked")
    older, _ = product.merge("web/first.js", status="checked")
    _, web = product.merge("web/again.js")
    product.record(older, web)  # a task merged twice waits by its newest merge
    _, api = product.merge("api/handlers.py")
    revert = product.commit("api/handlers.py")
    third, docs = product.merge("docs/notes.md")
    world.with_settings(rounds__bundle_merges=cap)

    answer = bundle.show(world.ctx(), {}, None)

    assert [m["task"] for m in answer["merges"]] == [web, api, docs][:count]
    assert answer["walk_at"] == [revert, third][count - 2]  # a revert after the bundle's last merge is in its walk
    assert answer["base"] == older and answer["waiting"] == 3
    assert answer["scenarios"] == [
        {"name": "api", "items": 1, "merges": [api]},
        {"name": "web", "items": 1, "merges": [web]},
    ]


@pytest.mark.parametrize(
    ("changed", "named", "count"),
    [(["web/page.js", "api/handlers.py"], [("api", 3), ("web", 1)], 4), (["docs/notes.md"], [], 3)],
    ids=["the-scenarios-its-merges-touch", "the-one-walked-instead"],
)
def test_a_bundle_names_the_items_each_scenario_takes_and_their_sum_as_the_count_of_a_brief_of_them_all(
    world: World, product: Product, changed: list[str], named: list[tuple[str, int]], count: int
) -> None:
    (world.repo / FOLDER / "api.md").write_text(f"---\n{SCENARIOS['api']}\nitems: 3\n---\n# api\n\n1. Open it.\n")
    product.git("commit", "-q", "-am", "api takes three items")
    for rel in changed:
        product.merge(rel)

    answer = bundle.show(world.ctx(), {}, None)

    assert [(s["name"], s["items"]) for s in answer["scenarios"]] == named
    assert f"one brief for them all says Scenarios and checks: {count}, plus one for each check" in answer["message"]


def test_a_full_pass_of_every_scenario_a_bundle_protects_clears_every_merge_up_to_its_commit_at_once(
    product: Product,
) -> None:
    _, web = product.merge("web/page.js")
    _, api = product.merge("api/handlers.py")
    tip, docs = product.merge("docs/notes.md")
    product.slot0(tip)

    answer = product.check(tip[:9], web="pass", api="pass")

    assert answer["cleared"] == [web, api, docs]
    assert {product.status(t) for t in (web, api, docs)} == {"checked"}


@pytest.mark.parametrize(
    ("rows", "runs", "key", "said"),
    [
        ({"web": "pass", "api": "fail"}, "tip", "next", "'api'"),
        ({"web": "pass", "api": "blocked"}, "tip", "next", "'api'"),
        ({"web": "pass"}, "tip", "why", "no row for api"),
        ({"web": "pass", "api": "pass"}, "first", "why", "slot 0's last probe does not name"),
    ],
    ids=["a row failed", "a row blocked", "a scenario left out", "slot 0 ran another commit"],
)
def test_a_check_clears_no_merge_unless_it_passed_in_full_at_the_commit_slot_0_ran(
    product: Product, rows: dict[str, str], runs: str, key: str, said: str
) -> None:
    first, web = product.merge("web/page.js")
    tip, api = product.merge("api/handlers.py")
    product.slot0({"tip": tip, "first": first}[runs])

    answer = product.check(tip, **rows)

    assert answer["cleared"] == [] and said in str(answer[key])
    assert (product.status(web), product.status(api)) == ("merged", "merged")
    assert [c.commit for c in Queue.of(product.world.ctx()).checks()[0]] == [tip]  # saved all the same


@pytest.mark.parametrize(
    ("named", "said"),
    [("branch", "is not on master's first-parent line"), ("unknown", "the checkout has no commit fedcba9")],
)
def test_a_check_names_a_commit_on_the_default_branch_s_line_or_is_refused_unsaved(
    product: Product, named: str, said: str
) -> None:
    product.merge("web/page.js")
    on_branch = product.git("rev-parse", "HEAD^2")

    with pytest.raises(Refused, match=said):
        product.check({"branch": on_branch, "unknown": "fedcba9"}[named], web="pass")
    assert Queue.of(product.world.ctx()).checks()[0] == []


def test_a_git_failure_naming_the_check_s_commit_is_refused_and_logged(
    world: World, product: Product, caplog: pytest.LogCaptureFixture
) -> None:
    world.launcher.git = lambda argv: (128, "fatal: cannot change to '/repo': No such file or directory")
    with caplog.at_level(logging.WARNING), pytest.raises(Refused, match="git rev-parse failed"):
        product.check("abc1234", web="pass")
    assert [r.getMessage() for r in caplog.records] == [
        "warden: git rev-parse in the agents' checkout failed (exit 128)"
    ]


@pytest.mark.parametrize(
    ("result", "earlier", "then"),
    [
        ("blocked", [], "walk it again"),
        ("fail", [], "walk it at the base"),
        ("fail", [("base", "fail")], "wait for its fix"),
        ("fail", [("base", "pass")], "walk it halfway"),
        ("fail", [("base", "pass"), ("first", "pass")], "walk it at the second again"),
        ("fail", [("base", "pass"), ("first", "pass"), ("second", "fail")], "revert the second"),
        ("fail", [("base", "blocked")], "walk it at the first"),
        ("fail", [("base", "blocked"), ("first", "fail")], "wait for its fix"),
        ("fail", [("base", "blocked"), ("first", "pass"), ("second", "fail")], "revert the second"),
        ("fail", [("base", "blocked"), ("first", "blocked"), ("second", "blocked")], "wait for its fix"),
        ("fail", [("first", "blocked")], "walk it at the second"),
    ],
    ids=[
        "blocked",
        "never passed",
        "fails at the base",
        "two suspects",
        "one suspect failed once",
        "one suspect failed twice",
        "the base cannot come up",
        "fails at the first it came up at",
        "passes at the first it came up at",
        "came up at none before",
        "no older commit comes up either",
    ],
)
def test_a_row_that_did_not_pass_names_what_comes_next(
    product: Product, result: str, earlier: list[tuple[str, str]], then: str
) -> None:
    first, one = product.merge("web/page.js")
    second, two = product.merge("web/list.js")
    tip, _ = product.merge("api/handlers.py")
    for at, said in earlier:
        product.check({"base": product.base, "first": first, "second": second}[at], web=said)

    answer = product.check(tip, web=result, api="pass")

    expected: dict[str, Any] = {
        "walk it again": {"do": "again"},
        "walk it at the base": {"do": "walk", "at": product.base},
        "walk it at the first": {"do": "walk", "at": first},
        "walk it at the second": {"do": "walk", "at": second},
        "wait for its fix": {"do": "wait"},
        "walk it halfway": {"do": "walk", "at": first, "suspects": [one, two]},
        "walk it at the second again": {"do": "walk", "at": second, "suspects": [two]},
        "revert the second": {"do": "revert", "merge": second, "task": two},
    }[then]
    told = [{k: v for k, v in step.items() if k != "why"} for step in answer["next"]]
    assert told == [{"scenario": "web", "result": result, **expected}]


@pytest.mark.parametrize(
    ("web", "changes", "gets_it"),
    [("fail", "web/fix.js", True), ("fail", "api/other.py", False), ("blocked", "web/fix.js", None)],
    ids=["a-fix-of-what-failed", "a-task-that-fixes-nothing-failed", "slot-0-could-not-come-up"],
)
def test_while_the_cap_of_merges_waits_a_merge_turn_goes_only_to_a_fix_of_a_scenario_their_check_failed(
    world: World, product: Product, monkeypatch: pytest.MonkeyPatch, web: str, changes: str, gets_it: bool | None
) -> None:
    world.with_settings(rounds__bundle_merges=2)
    product.merge("web/page.js")
    tip, _ = product.merge("api/handlers.py")
    ready = world.task(branch=product.branch(changes))
    world.queue().write_task(dataclasses.replace(ready, status="qa-clean", run=1))
    world.session("s-impl", "implementer", ready.branch, ready.id)
    monkeypatch.setattr(backstop, "processes", lambda: {})

    with pytest.raises(Refused, match="2 merges wait for their check, the cap of 2") as refused:
        bundle.next_turn(world.ctx(), {}, None)
    assert refused.value.rule == "bundle-full"

    product.check(tip, web=web, api="pass" if web == "fail" else web)
    if gets_it is None:
        with pytest.raises(Refused, match="check their bundle first"):
            bundle.next_turn(world.ctx(), {}, None)
        return
    answer = bundle.next_turn(world.ctx(), {}, None)
    assert answer["task"] == (ready.id if gets_it else None)
    assert gets_it or "only a fix of what their check failed (web)" in answer["message"]


@pytest.mark.parametrize("result", ["fail", "blocked"], ids=["a-fix-joins", "slot-0-cannot-build-the-cap-s-commit"])
def test_a_bundle_grows_past_the_cap_only_once_the_check_of_the_cap_did_not_pass(
    world: World, product: Product, result: str
) -> None:
    world.with_settings(rounds__bundle_merges=2)
    _, web = product.merge("web/page.js")
    capped, api = product.merge("api/handlers.py")
    fix, fixed = product.merge("web/fix.js")

    with pytest.raises(Refused, match="past the cap of 2"):
        bundle.show(world.ctx(), {"through": fix}, None)
    product.slot0(fix)
    assert "past the cap of 2" in product.check(fix, web="pass", api="pass")["why"]  # walked past it all the same
    product.check(capped, web=result, api="pass" if result == "fail" else result)
    for args in ({"through": fix[:9]}, {}):  # unasked, it runs through the tip
        answer = bundle.show(world.ctx(), args, None)
        assert [m["task"] for m in answer["merges"]] == [web, api, fixed] and answer["walk_at"] == fix

    product.slot0(fix)
    assert product.check(fix, web="pass", api="pass")["cleared"] == [web, api, fixed]


@pytest.mark.parametrize(
    ("built", "name", "said"),
    [
        (True, "web", "1. Open it."),
        (True, "phone-width", "1. Narrow the window."),
        (True, "README", "# Scenarios"),
        (False, "web", "slot 0 gives no build"),
        (True, "gone", "has no scenario or qa procedure gone"),
    ],
    ids=[
        "as-slot-0-runs-it",
        "a-qa-procedure-one-points-at",
        "the-scenarios-readme",
        "no-build-known",
        "one-its-build-lacks",
    ],
)
def test_a_regression_tester_reads_each_scenario_as_the_build_slot_0_runs_has_it(
    world: World, product: Product, monkeypatch: pytest.MonkeyPatch, built: bool, name: str, said: str
) -> None:
    (world.repo / "qa" / "phone-width.md").write_text("# Phone width\n\n1. Narrow the window.\n")
    product.git("add", "-A")
    product.git("commit", "-q", "-m", "a procedure")
    older = product.git("rev-parse", "HEAD")
    (world.repo / FOLDER / "web.md").write_text(f"---\n{SCENARIOS['web']}\n---\n# web\n\n1. Rewritten since.\n")
    product.git("commit", "-q", "-am", "rewrite web")
    # The deploy log lags slot 0 by up to a probe: slot 0's own answer is the build its testers walk.
    deploys.record(world.places, "staging", product.git("rev-parse", "HEAD")[:8], world.clock.iso())
    port = leases.port(world.settings, 0)
    world.launcher.stack = lambda n, verb: StackResult(0 if built else 1, f"http://127.0.0.1:{port}/health\n", "/l")
    monkeypatch.setattr(health, "http_get", lambda *_: json.dumps({"build_commit": older[:8]}).encode())
    world.session("s-orch", "orchestrator", "orchestrator", worktree=str(world.repo))
    tester = world.ctx(agent_id="a1", agent_kind="qa-regression")

    if not built or name == "gone":
        with pytest.raises(Refused, match=said):
            walked.show(tester, {"name": name}, None)
        return
    shown = walked.show(tester, {"name": name}, None)
    answer = shown["message"]
    assert answer.startswith(f"{name}, as slot 0 runs it at {older[:8]}") and said in answer
    assert "Rewritten" not in answer
    assert shown["folder"] == str(world.repo / ".factory" / "a1")  # the one folder the guard lets it write in
