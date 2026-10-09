"""QA (factory spec, "QA"): the fixed tester table as a strict union, ready records on the head the slot runs, and
qa-clean refused until every required tester reported that head clean."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from queue_kit import World

from runtime.qa import plan, qa_browser
from runtime.qa import verbs as qa
from runtime.qa.scenarios import parse
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
        ("---\nprotects: web/a\nneeds: nothing\nruns: web\n---\n# A\n" + "".join(f"{n}. x\n" for n in range(1, 8)),
         "7 steps"),
        ("---\nprotects: web/a\nneeds: nothing\n---\n# A\n1. x\n", "exactly protects"),
        ("---\nprotects: web/a\nneeds: nothing, sign-in\nruns: web\n---\n# A\n1. x\n", "nothing beside"),
        ("---\nprotects: web/a\nneeds: nothing\nruns: phone\n---\n# A\n1. x\n", "runs 'phone'"),
    ],
)  # fmt: skip
def test_a_scenario_keeps_the_standard_format(text: str, problem: str) -> None:
    scenario, problems = parse("a", text, 6)
    assert scenario is None and any(problem in p for p in problems)


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


def _result(world: World, agent: str, majors: int = 0) -> None:
    args = {"head": HEAD[:9], "blockers": 0, "majors": majors, "minors": 2}
    qa.qa_result(world.ctx("implementer", IMPL_ID, agent), args, None)


def test_qa_clean_waits_for_every_required_tester_on_the_head(ready_world: World) -> None:
    world, impl = ready_world, ready_world.ctx("implementer", IMPL_ID)
    answer = qa.qa_ready(impl, {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    assert [t["tester"] for t in answer["testers"]] == ["qa-ui-function", "qa-ux", "qa-visual", "qa-backend"]
    assert world.queue().task("t-0001").status == "qa"
    for agent in ("ag-ux", "ag-vis", "ag-fn"):
        _result(world, agent)
    with pytest.raises(Refused, match="no result from qa-backend"):
        qa.qa_clean(impl, {}, None)
    _result(world, "ag-be", majors=1)
    with pytest.raises(Refused, match="open blockers or majors from qa-backend"):
        qa.qa_clean(impl, {}, None)
    _result(world, "ag-be")
    qa.qa_clean(impl, {}, None)
    assert world.queue().task("t-0001").status == "qa-clean"


def test_qa_ready_needs_the_slot_running_the_head(ready_world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "http_get", lambda url, timeout, limit: b'{"build_commit": "bbbbbbb"}')
    with pytest.raises(Refused, match="your slot runs bbbbbbb"):
        qa.qa_ready(ready_world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    assert not (ready_world.places.state / "public" / "ready" / f"{IMPL_ID}.json").exists()


def test_only_a_registered_tester_reports_a_result(ready_world: World) -> None:
    qa.qa_ready(ready_world.ctx("implementer", IMPL_ID), {"head": HEAD[:12], "slices": [HEAD[:7]]}, None)
    with pytest.raises(Refused, match="registered tester"):
        _result(ready_world, "")
