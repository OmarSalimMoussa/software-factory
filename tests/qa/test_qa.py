"""QA (factory spec, "QA"): the fixed tester table as a strict union, ready records on the head the slot runs, and
qa-clean refused until every required tester reported that head clean."""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from queue_kit import World

from runtime.guard.facts import read_ready
from runtime.qa import findings, plan, qa_browser, steps
from runtime.qa import verbs as qa
from runtime.qa.scenarios import Step, parse
from runtime.shared import lanes, store
from runtime.shared.protocol import Refused
from runtime.slots import health, leases
from runtime.slots.verbs import slot
from runtime.warden.context import StackResult
from runtime.warden.sessions import registry

HEAD = "a" * 40
IMPL_ID = "0b6f1c3e-1d2a-4c5b-9e8f-7a6b5c4d3e2f"  # Claude Code names sessions by a UUID
PREFIXES = 'ui = ["web/"]\napi = ["server/"]\naccess = ["server/auth/"]\n'
# The scenarios at HEAD, by their numbered steps, as `git grep -n` prints them.
SCENARIOS = "".join(f"{HEAD}:qa/scenarios/{name}.md:{n}:{n}. Walk.\n" for name, steps in (("cart", 6), ("sign-in", 3))
                    for n in range(1, steps + 1))  # fmt: skip


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


def test_a_testers_browser_lets_every_page_use_what_the_setting_names(world: World, tmp_path: Path) -> None:
    world.with_settings(qa__browser_permissions=["clipboard-read", "clipboard-write"])
    at = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    config = qa_browser.prepare("qa-regression", IMPL_ID, tmp_path / "runs", world.ctx().settings(), at)
    context = json.loads(config.read_text())["browser"]["contextOptions"]
    assert context["permissions"] == ["clipboard-read", "clipboard-write"]


def test_a_testers_second_browser_is_a_context_of_its_own_in_the_first_ones_process_and_writes_apart(
    world: World, tmp_path: Path
) -> None:
    at = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    config = qa_browser.prepare("qa-ui-function-2", IMPL_ID, tmp_path / "runs", world.ctx().settings(), at)
    argv, where = qa_browser.second_command(config.parent, "qa-ui-function", world.ctx().settings())("/tmp/b.sock")
    first, second = (json.loads(path.read_text()) for path in (config, where / "playwright.json"))
    assert where == config.parent / "second" and argv[argv.index("--config") + 1] == str(where / "playwright.json")
    assert (second["browser"].pop("remoteEndpoint"), second.pop("outputDir"), first.pop("outputDir")) == (
        "/tmp/b.sock",
        str(where),
        str(config.parent),
    )
    assert second == first  # the same blocks, window and permissions, and screenshots seen by the same testers


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
    scenario, problems = parse("a", text, 6, 10)
    assert scenario is None and any(problem in p for p in problems)


def test_a_scenarios_steps_carry_their_names_and_the_first_thing_each_expects() -> None:
    text = ("---\nprotects: web/a\nneeds: nothing\nruns: in-turn\n---\n# A\n1. **Open the cart.** Go to /cart.\n"
            "   Expect: the cart shows\n2. Pay.\n   - Expected: a receipt\n   Expect: not this one\n"
            "3. **Leave** it.\n")  # fmt: skip
    scenario, problems = parse("a", text, 6, 10)
    assert problems == [] and scenario is not None
    assert scenario.steps == (Step("Open the cart", "the cart shows"), Step("", "a receipt"), Step("Leave", ""))


def _git(answers: dict[str, str]) -> Callable[[list[str]], tuple[int, str]]:
    """git, answering each subcommand from ``answers``; the warden's removal of a run it took succeeds."""
    return lambda argv: (0, "" if argv[0] == "rm" else answers[argv[3]])


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

    answers = {"rev-parse": HEAD, "show": PREFIXES, "diff": "web/page.ts\n", "merge-base": "", "grep": SCENARIOS}
    world.launcher.git = _git(answers)
    world.launcher.stack = lambda n, verb: StackResult(0, f"http://127.0.0.1:{8100 + n}/health\n", "/log")
    monkeypatch.setattr(health, "http_get", lambda url, timeout, limit: json.dumps({"build_commit": HEAD}).encode())
    monkeypatch.setattr(qa_browser, "runs_root", lambda: tmp_path / "runs")
    return world


def _walk(world: World, agent: str, widths: list[int]) -> None:
    """A browser run of the tester ``agent``, whose trace shows the page at each of ``widths``."""
    at = datetime.fromtimestamp(world.clock.now(), UTC)
    lane = registry.read(world.places)[agent].started_as
    run = qa_browser.prepare(lane, IMPL_ID, qa_browser.runs_root(), world.ctx().settings(), at).parent
    (run / "traces").mkdir()
    frames = [{"type": "screencast-frame", "sha1": f"page@1-{n}.jpeg", "width": w, "height": 900}
              for n, w in enumerate(widths)]  # fmt: skip
    (run / "traces" / "trace-1.trace").write_text("".join(json.dumps(frame) + "\n" for frame in frames))


def _result(world: World, agent: str, majors: int = 0) -> list[str]:
    """The tester ``agent`` records ``majors`` major findings and two minor ones, then its verdict; the majors' ids."""
    session = registry.read(world.places).get(agent)
    if session is not None and session.started_as in world.settings.get_list("subagents.browser_personas"):
        _walk(world, agent, [int(w) for w in world.settings.get_list("qa.widths")])
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


def _step(world: World, agent: str, mark: str, result: str) -> None:
    """The tester ``agent`` walks ``mark``'s item in order up to it: ``result`` at it, blocked before it."""
    ctx, (item, n) = world.ctx("implementer", IMPL_ID, agent), mark.split(":")
    for step in range(1, int(n) + 1):
        qa.qa_step(ctx, {"head": HEAD[:9], "item": item, "step": step}, None)
        said = {"result": result if step == int(n) else "blocked", "got": "seen"}
        qa.qa_step_result(ctx, {"head": HEAD[:9], "item": item, "step": step, **said}, None)


@pytest.mark.parametrize(
    ("again", "filed", "refused"),
    [
        (None, None, "walks that step on aaaaaaaaa again and it passes, or name the request that tracks it"),
        (("ag-fn2", "sign-in:2", "pass"), None, None),
        (("ag-fn2", "sign-in:3", "pass"), None, "walks that step"),
        (("ag-fn2", "sign-in:2", "fail"), None, "walks that step"),
        (("ag-fn", "sign-in:2", "pass"), None, "walks that step"),
        (None, "older", None),
        (None, "own", "is t-0001's own request"),
        (None, "r-2026-10-09-0099", "no request r-2026-10-09-0099"),
    ],
    ids=["neither", "a-later-tester-passed-it", "another-step", "failed-again", "the-same-testers-retry",
         "an-older-faults-request", "the-tasks-own-request", "no-such-request"],
)  # fmt: skip
def test_a_finding_at_a_step_is_waived_once_a_later_tester_passed_it_or_a_request_tracks_it_as_older(
    ready_world: World, again: tuple[str, str, str] | None, filed: str | None, refused: str | None
) -> None:
    # sr328: 'typed keys never showed' was waived as a missed aim, and the next round showed a real defect.
    world = ready_world
    qa.qa_ready(world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    world.session(
        "ag-fn2", "qa-ui-function", "impl-t-0001.qa-ui-function-2", "t-0001", agent_id="ag-fn2", parent=IMPL_ID
    )
    _step(world, "ag-fn", "sign-in:2", "fail")
    args = {"head": HEAD[:9], "severity": "major", "where": "/sign-in", "step": "sign-in:2"}
    finding = qa.qa_finding(world.ctx("implementer", IMPL_ID, "ag-fn"), args, b"Typed keys never showed.")["finding"]
    world.clock.at += 60
    if again is not None:
        _step(world, *again)
    named = {"older": world.request().id, "own": world.queue().task("t-0001").requests[0]}.get(filed or "", filed)
    ruling = {"task": "t-0001", "finding": finding, "why": "an older fault", **({"filed": named} if named else {})}

    if refused is not None:
        with pytest.raises(Refused, match=refused):
            qa.qa_waive(world.ctx(), ruling, None)
    else:
        qa.qa_waive(world.ctx(), ruling, None)
    assert findings.open_count(world.ctx(), "t-0001", HEAD, "qa-ui-function") == (1 if refused else 0)


def test_a_blocked_verdict_holds_qa_clean_until_the_tester_walks_again(ready_world: World) -> None:
    world, impl = ready_world, ready_world.ctx("implementer", IMPL_ID)
    qa.qa_ready(impl, {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    for agent in ("ag-ux", "ag-vis", "ag-be"):
        _result(world, agent)
    qa.qa_result(world.ctx("implementer", IMPL_ID, "ag-fn"), {"head": HEAD[:9], "blocked": "no sign-in link"}, None)

    with pytest.raises(Refused, match=r"qa-ui-function could not walk it \(no sign-in link\)"):
        qa.qa_clean(impl, {}, None)
    # A verdict ends a tester's walk: a new one walks again.
    world.session("ag-fn2", "qa-ui-function", "impl-t-0001.qa-ui-function-ab12cd", "t-0001", agent_id="ag-fn2",
                  parent=IMPL_ID)  # fmt: skip
    _result(world, "ag-fn2")
    qa.qa_clean(impl, {}, None)


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


WHY = "the slot's sign-in printed no link"


@pytest.mark.parametrize(
    ("agent", "shown", "excuse", "surfaces", "changed", "refused"),
    [
        ("ag-fn", [1440, 1280, 1024, 390], "", ["desktop"], "web/page.ts", None),
        ("ag-fn", [1440, 1280], "", ["desktop"], "web/page.ts", "1024, 390"),
        ("ag-vis", None, "", ["ui"], "", "1440, 1024, 390"),
        ("ag-ux", [1440], "major", ["ui"], "", None),
        ("ag-ux", [1440], "blocked", ["ui"], "", None),
        ("ag-fn", [1280], "", ["desktop"], "ext/panel.js", None),
        ("ag-fn", [1440], "reproduce", ["ui"], "", None),
        ("ag-be", None, "", ["ui"], "", None),
    ],
    ids=["every-width", "two-missed", "no-walk", "failed-with-a-finding", "blocked-with-why", "off-the-screens",
         "a-reproduction", "no-browser"],
)  # fmt: skip
def test_a_browser_testers_clean_verdict_on_the_screens_waits_for_its_walk_at_every_width(
    ready_world: World, agent: str, shown: list[int] | None, excuse: str, surfaces: list[str], changed: str,
    refused: str | None,
) -> None:  # fmt: skip
    world = ready_world
    world.with_settings(qa__widths=[1440, 1024, 390])
    world.queue().write_task(dataclasses.replace(world.queue().task("t-0001"), surfaces=surfaces))
    answers = {"rev-parse": HEAD, "show": PREFIXES, "diff": f"{changed}\n", "merge-base": "", "log": "docs/a.md\n"}
    world.launcher.git = _git(answers)
    ready = {"reproduce": True} if excuse == "reproduce" else {"slices": [HEAD[:7]]}
    qa.qa_ready(world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], **ready}, None)
    ctx = world.ctx("implementer", IMPL_ID, agent)
    if shown is not None:
        _walk(world, agent, shown)
    if excuse == "major":
        qa.qa_finding(ctx, {"head": HEAD[:9], "severity": "major", "where": "/cart"}, b"It breaks.")
    args = {"head": HEAD[:9], **({"blocked": WHY} if excuse == "blocked" else {})}

    if refused is not None:
        with pytest.raises(Refused, match=f"never shows the page at {refused} px wide"):
            qa.qa_result(ctx, args, None)
        assert not store.read_jsonl(world.places.results_of(IMPL_ID))
        _walk(world, agent, [int(w) for w in refused.split(", ")])
    qa.qa_result(ctx, args, None)

    [row] = store.read_jsonl(world.places.results_of(IMPL_ID))
    assert row.get("blocked") == (WHY if excuse == "blocked" else None)


def test_a_round_on_the_screens_gives_each_browser_testers_brief_the_widths_its_hand_back_checks(
    ready_world: World,
) -> None:
    # sr407: a task naming the screens, with a diff off them; its brief said no widths item, and its hand-back refused.
    world = ready_world
    world.with_settings(qa__widths=[1440, 1024, 390])
    world.launcher.git = _git({"rev-parse": HEAD, "show": PREFIXES, "diff": "ext/panel.js\n", "merge-base": ""})
    qa.qa_ready(world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)

    [record] = read_ready(world.places, IMPL_ID)
    told = {kind: lanes.checked_widths(world.settings, record, kind) for kind in ("qa-ui-function", "qa-backend")}
    assert told == {"qa-ui-function": [1440, 1024, 390], "qa-backend": []}
    with pytest.raises(Refused, match="never shows the page at 1440, 1024, 390 px wide"):
        qa.qa_result(world.ctx("implementer", IMPL_ID, "ag-fn"), {"head": HEAD[:9]}, None)


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
    ready_world.launcher.git = _git(answers)
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


def test_a_slot_its_session_stopped_for_a_set_up_stays_ready_on_the_commit_it_ran_until_its_stack_changes(
    ready_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    impl, tester = (
        ready_world.ctx("implementer", IMPL_ID),
        ready_world.ctx("implementer", IMPL_ID, "ag-be", "qa-backend"),
    )
    qa.qa_ready(impl, {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    slot(impl, {"op": "stop", "services": ["relay"]}, None)
    monkeypatch.setattr(health, "http_get", _no_answer)
    slot(impl, {"op": "stop", "services": ["control-plane"]}, None)

    assert qa.ready_check(tester, {}, None)["stopped"].startswith("control-plane, relay, by your implementer")
    slot(impl, {"op": "up", "services": ["relay"]}, None)
    with pytest.raises(Refused, match="gave no commit"):
        qa.ready_check(tester, {}, None)


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
    ready_world.launcher.git = _git(answers)
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
    ended = {"head": HEAD[:9], "item": "cart", "step": 1, "result": "fail", "got": "the total\nstayed"}
    with pytest.raises(Refused, match="start step 1 of cart first"):
        qa.qa_step_result(ctx, ended, None)
    qa.qa_step(ctx, {"head": HEAD[:9], "item": "cart", "step": 1}, None)
    qa.qa_step_result(ctx, ended, None)
    qa.qa_finding(ctx, {"head": HEAD[:9], "severity": "major", "where": "/cart", "step": "cart:1"}, b"It breaks.")

    marks = steps.of_agent(ready_world.ctx(), IMPL_ID, "ag-fn")
    assert [(m["item"], m["step"], m.get("result"), m.get("got")) for m in marks] == [
        ("cart", 1, None, None), ("cart", 1, "fail", "the total stayed")]  # fmt: skip
    assert [f["step"] for f in findings.of_agent(ready_world.ctx(), "t-0001", "ag-fn")] == ["cart:1"]


@pytest.mark.parametrize(
    ("verb", "args"),
    [
        (qa.qa_step, {"item": "cart", "step": 1}),
        (qa.qa_step_result, {"item": "cart", "step": 1, "result": "pass", "got": "it saved"}),
        (qa.qa_finding, {"severity": "major", "where": "/cart"}),
        (qa.qa_result, {}),
    ],
    ids=["a-step", "a-steps-result", "a-finding", "a-second-verdict"],
)
def test_a_testers_verdict_is_its_last_word_as_it_takes_its_runs_into_the_evidence(
    ready_world: World, verb: Callable[..., dict[str, Any]], args: dict[str, Any]
) -> None:
    qa.qa_ready(ready_world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    _result(ready_world, "ag-fn")
    with pytest.raises(Refused, match="your verdict is recorded"):
        verb(ready_world.ctx("implementer", IMPL_ID, "ag-fn"), {"head": HEAD[:9], **args}, b"It breaks.")
    other = ready_world.ctx("implementer", IMPL_ID, "ag-ux")
    if verb is qa.qa_step_result:
        qa.qa_step(other, {"head": HEAD[:9], "item": "cart", "step": 1}, None)
    if verb is qa.qa_result:
        _walk(ready_world, "ag-ux", [int(w) for w in ready_world.settings.get_list("qa.widths")])
    verb(other, {"head": HEAD[:9], **args}, b"It breaks.")


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
    ("agent", "item", "step", "refused"),
    [
        ("ag-fn", "cart", 6, None),
        ("ag-fn", "cart", 7, "cart has 6 steps at aaaaaaaaa: mark one from 1 to 6, and a check of your brief under "
                             "its own item, such as --step widths:1"),
        ("ag-be", "cart", 7, "cart has 6 steps at aaaaaaaaa: mark one from 1 to 6, and a check of your brief under "
                             "its own item"),
        ("ag-fn", "widths", 1, None),
        ("ag-fn", "widths", 2, "widths has no numbered steps at aaaaaaaaa, so it is one step: --step widths:1"),
        ("ag-be", "slow-total", 2, "slow-total has no numbered steps at aaaaaaaaa, so it is one step"),
    ],
    ids=["a-scenarios-last", "past-its-last", "past-its-last-with-no-widths", "a-check", "past-a-check",
         "past-a-hold"],
)  # fmt: skip
def test_a_step_is_one_of_its_items_own_numbered_steps(
    ready_world: World, agent: str, item: str, step: int, refused: str | None
) -> None:
    # sr425: a tester marked a width check as step 7 of a six-step scenario, and the factory took it.
    qa.qa_ready(ready_world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    if step > 1:
        _step(ready_world, agent, f"{item}:{step - 1}", "pass")
    mark = {"head": HEAD[:9], "item": item, "step": step}
    if refused is not None:
        with pytest.raises(Refused) as said:
            qa.qa_step(ready_world.ctx("implementer", IMPL_ID, agent), mark, None)
        assert said.value.reason.startswith(refused) and ("widths" in said.value.reason) == ("widths" in refused)
    else:
        qa.qa_step(ready_world.ctx("implementer", IMPL_ID, agent), mark, None)


@pytest.mark.parametrize(
    ("walked", "then", "refused"),
    [
        ([("ag-fn", 1, "pass")], ("ag-fn", 2), None),
        ([("ag-fn", 1, "blocked")], ("ag-fn", 2), None),
        ([], ("ag-fn", 3), "mark step 1 of cart first"),
        ([("ag-fn", 1, "pass")], ("ag-fn", 3), "mark step 2 of cart first"),
        ([("ag-fn", 1, "pass"), ("ag-fn", 2, "fail")], ("ag-fn", 2), None),
        ([("ag-fn", 1, "pass"), ("ag-fn", 2, "fail")], ("ag-fn", 1), None),
        ([("ag-ux", 1, "pass")], ("ag-fn", 2), "mark step 1 of cart first"),
    ],
    ids=["after-the-step-before", "after-a-blocked-step", "before-the-step-before-started", "past-a-gap",
         "a-re-walk", "a-re-walk-of-an-earlier-step", "another-testers-marks"],
)  # fmt: skip
def test_an_items_steps_start_in_order_once_the_step_before_has_the_testers_own_result(
    ready_world: World, walked: list[tuple[str, int, str]], then: tuple[str, int], refused: str | None
) -> None:
    # sr425: a tester marked step 4 without its start, then opened step 5, and the replay showed them out of order.
    qa.qa_ready(ready_world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    for agent, step, result in walked:
        ctx = ready_world.ctx("implementer", IMPL_ID, agent)
        qa.qa_step(ctx, {"head": HEAD[:9], "item": "cart", "step": step}, None)
        qa.qa_step_result(ctx, {"head": HEAD[:9], "item": "cart", "step": step, "result": result, "got": "seen"}, None)
    ctx = ready_world.ctx("implementer", IMPL_ID, then[0])
    if refused is not None:
        with pytest.raises(Refused, match=re.escape(refused)):
            qa.qa_step(ctx, {"head": HEAD[:9], "item": "cart", "step": then[1]}, None)
    else:
        qa.qa_step(ctx, {"head": HEAD[:9], "item": "cart", "step": then[1]}, None)


@pytest.mark.parametrize(
    ("verb", "args", "says"),
    [
        ("step", {"item": "Cart page", "step": 1}, "name the item as your brief does"),
        ("step", {"item": "cart", "step": 0}, "its number in the item"),
        ("step-result", {"item": "cart", "step": 1, "result": "ok", "got": "it showed"}, "pass, fail, blocked"),
        ("step-result", {"item": "cart", "step": 1, "result": "pass", "got": " "}, "--got"),
        ("finding", {"severity": "minor", "where": "/cart", "step": "cart"}, "<item>:<n>"),
        ("result", {"blocked": " "}, "--blocked"),
    ],
)
def test_a_step_mark_names_its_item_and_step_and_a_result_says_what_was_seen(
    ready_world: World, verb: str, args: dict[str, object], says: str
) -> None:
    qa.qa_ready(ready_world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    verbs = {"step": qa.qa_step, "step-result": qa.qa_step_result, "finding": qa.qa_finding, "result": qa.qa_result}
    send = verbs[verb]
    with pytest.raises(Refused, match=re.escape(says)):
        send(ready_world.ctx("implementer", IMPL_ID, "ag-fn"), {"head": HEAD[:9], **args}, b"It breaks.")
