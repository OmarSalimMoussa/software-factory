"""QA (factory spec, "QA"): the fixed tester table as a strict union, ready records on the head the slot runs, and
qa-clean refused until every required tester reported that head clean."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest
from queue_kit import World

from runtime.qa import findings, plan, qa_browser, steps
from runtime.qa import verbs as qa
from runtime.qa.scenarios import Step, parse
from runtime.shared.protocol import Refused
from runtime.slots import health, leases
from runtime.warden.context import StackResult

HEAD = "a" * 40
IMPL_ID = "0b6f1c3e-1d2a-4c5b-9e8f-7a6b5c4d3e2f"  # Claude Code names sessions by a UUID
PREFIXES = 'ui = ["web/"]\napi = ["server/"]\naccess = ["server/auth/"]\n'


@pytest.mark.parametrize(
    ("surfaces", "rung", "changed", "testers"),
    [
        (["api"], 1, [], []),  # a small backend fix runs no tester
        (["api"], 2, [], ["qa-backend"]),
        (["api"], 1, ["web/page.ts"], ["qa-ui-function", "qa-ux", "qa-visual"]),  # the diff adds what the task left out
        (["none"], 1, ["server/auth/token.py"], ["qa-security"]),
        (["desktop"], 1, [], ["qa-ui-function"]),
    ],
)
def test_the_required_testers_are_the_union_of_the_task_and_its_diff(
    surfaces: list[str], rung: int, changed: list[str], testers: list[str]
) -> None:
    found = plan.required(surfaces, rung, plan.diff_surfaces(changed, plan.surface_prefixes(PREFIXES)))
    assert list(found) == testers


def test_surfaces_toml_names_only_surfaces() -> None:
    with pytest.raises(plan.SurfacesError, match="nothing is not a surface"):
        plan.surface_prefixes('nothing = ["docs/"]')


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("---\nprotects: web/a\nneeds: nothing\nruns: in-turn\n---\n# A\n" + "".join(f"{n}. x\n" for n in range(1, 8)),
         "7 steps"),
        ("---\nprotects: web/a\nneeds: nothing\n---\n# A\n1. x\n", "not protects, needs and runs"),
        ("---\nprotects: web/a\nneeds: nothing, sign-in\nruns: in-turn\n---\n# A\n1. x\n", "nothing beside"),
        ("---\nprotects: web/a\nneeds: nothing\nruns: phone\n---\n# A\n1. x\n", "runs 'phone'"),
    ],
)  # fmt: skip
def test_a_scenario_keeps_the_standard_format(text: str, problem: str) -> None:
    scenario, problems = parse("a", text, 6)
    assert scenario is None and any(problem in p for p in problems)


def test_a_scenarios_steps_carry_their_names_and_the_first_thing_each_expects() -> None:
    text = ("---\nprotects: web/a\nneeds: nothing\nruns: in-turn\n---\n# A\n1. **Open the cart.** Go to /cart.\n"
            "   Expect: the cart shows\n2. Pay.\n   - Expected: a receipt\n   Expect: not this one\n"
            "3. **Leave** it.\n")  # fmt: skip
    scenario, problems = parse("a", text, 6)
    assert problems == [] and scenario is not None
    assert scenario.steps == (Step("Open the cart", "the cart shows"), Step("", "a receipt"), Step("Leave", ""))


@pytest.fixture
def ready_world(world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    task = world.task(surfaces=["ui", "api"], slices=1)
    world.queue().write_task(dataclasses.replace(task, status="building", run=2))
    world.session(IMPL_ID, "implementer", "impl-t-0001", task.id, "/wt/impl-t-0001")
    for agent, kind in (
        ("ag-ux", "qa-ux"),
        ("ag-vis", "qa-visual"),
        ("ag-fn", "qa-ui-function"),
        ("ag-be", "qa-backend"),
    ):
        world.session(agent, kind, f"{kind}-1", task.id, agent_id=agent)
    leases.claim(world.ctx("implementer", IMPL_ID), "impl-t-0001", "/wt/impl-t-0001")

    def git(argv: list[str]) -> tuple[int, str]:
        answers = {"rev-parse": HEAD, "show": PREFIXES, "diff": "web/page.ts\n", "merge-base": ""}
        return 0, answers[argv[3]]

    world.launcher.git = git
    world.launcher.stack = lambda n, verb: StackResult(0, f"http://127.0.0.1:{8100 + n}/health\n", "/log")
    monkeypatch.setattr(health, "http_get", lambda url, timeout, limit: json.dumps({"build_commit": HEAD}).encode())
    monkeypatch.setattr(qa_browser, "runs_root", lambda: tmp_path / "runs")
    return world


def _result(world: World, agent: str, majors: int = 0) -> list[str]:
    """The tester ``agent`` records ``majors`` major findings and two minor ones, then its verdict; the majors' ids."""
    ctx = world.ctx("implementer", IMPL_ID, agent)
    found = [qa.qa_finding(ctx, {"head": HEAD[:9], "severity": severity, "where": "/cart"}, b"It breaks.")["finding"]
             for severity in ["major"] * majors + ["minor"] * 2]  # fmt: skip
    verdict = qa.qa_result(ctx, {"head": HEAD[:9]}, None)
    assert (verdict["majors"], verdict["minors"]) == (majors, 2)
    return found[:majors]


def test_qa_clean_waits_for_every_required_tester_on_the_head(ready_world: World) -> None:
    world, impl = ready_world, ready_world.ctx("implementer", IMPL_ID)
    answer = qa.qa_ready(impl, {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    assert [t["tester"] for t in answer["testers"]] == ["qa-ui-function", "qa-ux", "qa-visual", "qa-backend"]
    assert world.queue().task("t-0001").status == "qa"
    for agent in ("ag-ux", "ag-vis", "ag-fn"):
        _result(world, agent)
    with pytest.raises(Refused, match="no result from qa-backend"):
        qa.qa_clean(impl, {}, None)
    disputed = _result(world, "ag-be", majors=1)
    with pytest.raises(Refused, match="open blockers or majors from qa-backend"):
        qa.qa_clean(impl, {}, None)
    qa.qa_waive(world.ctx(), {"task": "t-0001", "finding": disputed[0], "why": "the spec asks for it"}, None)
    qa.qa_clean(impl, {}, None)
    assert world.queue().task("t-0001").status == "qa-clean"


def test_qa_clean_waits_for_a_second_tester_of_a_kind_still_testing(ready_world: World) -> None:
    world, impl = ready_world, ready_world.ctx("implementer", IMPL_ID)
    world.session("ag-ux2", "qa-ux", "impl-t-0001.qa-ux-ab12cd", "t-0001", agent_id="ag-ux2", parent=IMPL_ID)
    qa.qa_ready(impl, {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    for agent in ("ag-ux", "ag-vis", "ag-fn", "ag-be"):
        _result(world, agent)

    # Its kind has a verdict from the first tester: the second, in its own browser lane, has not given its own.
    with pytest.raises(Refused, match="impl-t-0001.qa-ux-ab12cd still testing"):
        qa.qa_clean(impl, {}, None)
    _result(world, "ag-ux2")
    qa.qa_clean(impl, {}, None)


@pytest.mark.parametrize(
    ("changed", "slices", "refused"),
    [("", None, False), ("docs/specs/t-0001.md\n", None, False), ("docs/specs/t-0001.md\n\nweb/page.ts\n", None, True),
     ("", [HEAD[:7]], True)],
    ids=["before-the-first-commit", "after-its-spec-alone", "after-a-commit-beyond-markdown", "naming-a-slice"],
)  # fmt: skip
def test_a_reproduction_comes_before_the_first_commit_beyond_markdown_and_none_of_its_results_is_qa(
    ready_world: World, monkeypatch: pytest.MonkeyPatch, changed: str, slices: list[str] | None, refused: bool
) -> None:
    answers = {"rev-parse": HEAD, "show": PREFIXES, "diff": "web/page.ts\n", "merge-base": "", "log": changed}
    ready_world.launcher.git = lambda argv: (0, answers[argv[3]])
    impl = ready_world.ctx("implementer", IMPL_ID)
    args = {"head": HEAD[:12], "reproduce": True, **({"slices": slices} if slices else {})}
    if refused:
        with pytest.raises(Refused, match="before your first commit"):
            qa.qa_ready(impl, args, None)
        return
    qa.qa_ready(impl, args, None)
    _result(ready_world, "ag-fn")
    with pytest.raises(Refused, match="no ready record of your built slices"):
        qa.qa_clean(impl, {}, None)
    assert ready_world.queue().task("t-0001").status == "building"

    # The fix touches no screen, yet the reproduction's verdict on the screens never carries to its head.
    head2 = "b" * 40
    answers.update({"rev-parse": head2, "diff": "server/api/cart.py\n"})
    monkeypatch.setattr(health, "http_get", lambda url, timeout, limit: json.dumps({"build_commit": head2}).encode())
    qa.qa_ready(impl, {"head": head2[:12], "slices": [head2[:7]]}, None)
    with pytest.raises(Refused, match="no result from qa-ui-function"):
        qa.qa_clean(impl, {}, None)


def _no_answer(url: str, timeout: int, limit: int) -> bytes:
    raise ConnectionRefusedError("refused")


@pytest.mark.parametrize(
    ("printed", "answer", "refusal"),
    [
        ("http://127.0.0.1:8101/health", lambda *_: b'{"build_commit": "bbbbbbb"}', "your slot runs bbbbbbb"),
        ("http://127.0.0.1:8101/health", lambda *_: b'{"build_commit": "dev-slot1"}', "the stand-in its env file"),
        ("http://127.0.0.1:8101/health", _no_answer, "gave no commit (ConnectionRefusedError)"),
        # A bracketed host that is no address made the probe raise, and the warden answered "the warden failed".
        ("http://[redacted]:8101/health", lambda *_: b"{}", "must end with the slot's health address"),
        ("", lambda *_: b"{}", "health exited 0 with no address"),
    ],
)
def test_qa_ready_needs_the_slot_running_the_head(
    ready_world: World, monkeypatch: pytest.MonkeyPatch, printed: str, answer: object, refusal: str
) -> None:
    ready_world.launcher.stack = lambda n, verb: StackResult(0, f"{printed}\n", "/log")
    monkeypatch.setattr(health, "http_get", answer)
    with pytest.raises(Refused, match=re.escape(refusal)):
        qa.qa_ready(ready_world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    assert not (ready_world.places.state / "public" / "ready" / f"{IMPL_ID}.json").exists()


def test_only_a_registered_tester_reports_a_result(ready_world: World) -> None:
    qa.qa_ready(ready_world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    with pytest.raises(Refused, match="registered tester"):
        _result(ready_world, "")


def _log(*commits: tuple[str, str]) -> str:
    """git log's answer for --format=%H%x1f%B%x1e, newest first: each commit's sha and whole message."""
    return "".join(f"{sha}\x1f{message}\n\x1e\n" for sha, message in commits)


A, B, C = "a" * 40, "b" * 40, "c" * 40
COAUTHOR = "Co-Authored-By: Claude <noreply@anthropic.com>"


@pytest.mark.parametrize(
    ("log", "found"),
    [
        (_log((B, "Pay\n\nSlice: 2/2"), (A, "Cart\n\nSlice: 1/2")), [A, B]),
        (_log((C, "Pay again\n\nSlice: 2/2"), (B, "Pay\n\nSlice: 2/2"), (A, "Cart\n\nSlice: 1/2")), [A, C]),
        # The line sat in its own paragraph above the attribution, where git's trailer reader never looks.
        (_log((B, f"Pay\n\nSlice: 2/2\n\n{COAUTHOR}"), (A, f"Cart\n\nSlice: 1/2\n\n{COAUTHOR}")), [A, B]),
        (_log((A, "Cart\n\nSlice: 1/2")), "Slice: 2/2"),
        (_log((B, "Pay\n\nSlice: 2/2\nSlice: 1/2"), (A, "Cart\n\nSlice: 1/2")), "2 different Slice lines"),
        (_log((B, "Pay\n\nSlice: 2/3"), (A, "Cart\n\nSlice: 1/2")), "the task has 2 slices"),
    ],
    ids=["both-slices", "a-slice-redone", "attribution-after", "a-slice-missing", "two-places", "wrong-count"],
)
def test_qa_ready_finds_each_slices_commit_by_its_slice_line(
    ready_world: World, log: str, found: list[str] | str
) -> None:
    task = ready_world.queue().task("t-0001")
    ready_world.queue().write_task(dataclasses.replace(task, slices=2))
    answers = {"rev-parse": HEAD, "show": PREFIXES, "diff": "web/page.ts\n", "merge-base": "", "log": log}
    ready_world.launcher.git = lambda argv: (0, answers[argv[3]])
    impl = ready_world.ctx("implementer", IMPL_ID)

    if isinstance(found, str):
        with pytest.raises(Refused, match=re.escape(found)):
            qa.qa_ready(impl, {"head": HEAD[:12]}, None)
        return
    qa.qa_ready(impl, {"head": HEAD[:12]}, None)
    record = json.loads((ready_world.places.state / "public" / "ready" / f"{IMPL_ID}.json").read_text())["records"][-1]
    assert record["slices"] == found


@pytest.mark.parametrize(
    ("since", "rerun"),
    [("server/api/cart.py", ["qa-backend"]), ("web/cart.ts", ["qa-ui-function", "qa-ux", "qa-visual"])],
    ids=["a-backend-fix", "a-screen-fix"],
)
def test_a_clean_verdict_carries_to_the_next_head_unless_its_surfaces_changed(
    ready_world: World, monkeypatch: pytest.MonkeyPatch, since: str, rerun: list[str]
) -> None:
    world, impl = ready_world, ready_world.ctx("implementer", IMPL_ID)
    qa.qa_ready(impl, {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    for agent in ("ag-ux", "ag-vis", "ag-fn"):
        _result(world, agent)
    _result(world, "ag-be", majors=1)

    head2 = "b" * 40
    answers = {"rev-parse": head2, "show": PREFIXES, "merge-base": ""}

    def git(argv: list[str]) -> tuple[int, str]:
        if argv[3] != "diff":
            return 0, answers[argv[3]]
        # The fix since the first round touches one surface; the branch's whole diff still touches both.
        return 0, f"{since}\n" if HEAD in argv else "web/page.ts\nserver/api/cart.py\n"

    world.launcher.git = git
    monkeypatch.setattr(health, "http_get", lambda url, timeout, limit: json.dumps({"build_commit": head2}).encode())
    qa.qa_ready(impl, {"head": head2[:12], "slices": [head2[:7]]}, None)

    with pytest.raises(Refused) as refused:
        qa.qa_clean(impl, {}, None)
    assert f"no result from {', '.join(rerun)}" in refused.value.reason


def test_a_testers_steps_are_kept_in_order_and_a_result_needs_its_start(ready_world: World) -> None:
    qa.qa_ready(ready_world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    ctx = ready_world.ctx("implementer", IMPL_ID, "ag-fn")
    ended = {"head": HEAD[:9], "item": "cart", "step": 2, "result": "fail", "got": "the total\nstayed"}
    with pytest.raises(Refused, match="start step 2 of cart first"):
        qa.qa_step_result(ctx, ended, None)
    qa.qa_step(ctx, {"head": HEAD[:9], "item": "cart", "step": 2}, None)
    qa.qa_step_result(ctx, ended, None)
    qa.qa_finding(ctx, {"head": HEAD[:9], "severity": "major", "where": "/cart", "step": "cart:2"}, b"It breaks.")

    marks = steps.of_agent(ready_world.ctx(), IMPL_ID, "ag-fn")
    assert [(m["item"], m["step"], m.get("result"), m.get("got")) for m in marks] == [
        ("cart", 2, None, None), ("cart", 2, "fail", "the total stayed")]  # fmt: skip
    assert [f["step"] for f in findings.of_agent(ready_world.ctx(), "t-0001", "ag-fn")] == ["cart:2"]


def test_a_tester_starts_an_items_step_only_once_its_earlier_step_ended_while_another_items_may_run_inside(
    ready_world: World,
) -> None:
    qa.qa_ready(ready_world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    ctx, other = (ready_world.ctx("implementer", IMPL_ID, agent) for agent in ("ag-fn", "ag-ux"))
    qa.qa_step(ctx, {"head": HEAD[:9], "item": "cart", "step": 1}, None)
    qa.qa_step(other, {"head": HEAD[:9], "item": "cart", "step": 1}, None)
    qa.qa_step(ctx, {"head": HEAD[:9], "item": "slow-total", "step": 1}, None)  # a hold, inside cart's step 1
    with pytest.raises(Refused, match="mark how step 1 of cart ended first"):
        qa.qa_step(ctx, {"head": HEAD[:9], "item": "cart", "step": 2}, None)
    qa.qa_step_result(ctx, {"head": HEAD[:9], "item": "cart", "step": 1, "result": "pass", "got": "it showed"}, None)
    qa.qa_step(ctx, {"head": HEAD[:9], "item": "cart", "step": 2}, None)

    assert [m["step"] for m in steps.of_agent(ready_world.ctx(), IMPL_ID, "ag-fn")] == [1, 1, 1, 2]


@pytest.mark.parametrize(
    ("verb", "args", "says"),
    [
        ("step", {"item": "Cart page", "step": 1}, "name the item as your brief does"),
        ("step", {"item": "cart", "step": 7}, "from 1 to 6"),
        ("step-result", {"item": "cart", "step": 1, "result": "ok", "got": "it showed"}, "pass, fail, blocked"),
        ("step-result", {"item": "cart", "step": 1, "result": "pass", "got": " "}, "--got"),
        ("finding", {"severity": "minor", "where": "/cart", "step": "cart"}, "<item>:<n>"),
    ],
)
def test_a_step_mark_names_its_item_and_step_and_a_result_says_what_was_seen(
    ready_world: World, verb: str, args: dict[str, object], says: str
) -> None:
    qa.qa_ready(ready_world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    send = {"step": qa.qa_step, "step-result": qa.qa_step_result, "finding": qa.qa_finding}[verb]
    with pytest.raises(Refused, match=re.escape(says)):
        send(ready_world.ctx("implementer", IMPL_ID, "ag-fn"), {"head": HEAD[:9], **args}, b"It breaks.")
