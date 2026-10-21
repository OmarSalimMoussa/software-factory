"""Prod's and slot 0's unresolved GlitchTip issues become requests from glitchtip: each issue once, and the factory's
own never. Slot 0 is staging."""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from loop_fakes import World

from runtime.queue import formats
from runtime.queue.book import Queue, request_rel, task_rel
from runtime.shared import store
from runtime.shared.models import Session, Task
from runtime.shared.network import Answer
from runtime.warden.health import errors_import, telemetry
from runtime.warden.sessions import doing, registry
from runtime.warden.verbs import REGRESSION

GLITCHTIP = "https://box.test:8200"
SERVER = {"glitchtip": {"url": GLITCHTIP, "org": "notes", "token": "tok-read"}}


def issue(n: int, project: str) -> dict[str, Any]:
    return {"id": str(n), "title": f"KeyError: 'note' ({n})", "culprit": "app.notes in show", "level": "error",
            "firstSeen": "2026-10-09T10:00:00Z", "lastSeen": "2026-10-09T11:00:00Z", "count": "3",
            "permalink": f"{GLITCHTIP}/{project}/issues/{n}",
            "project": {"id": "1", "slug": project, "name": project}}  # fmt: skip


# GlitchTip's Link header: the first page names a next one; the last names only a previous one, never followed.
FIRST = f'<{GLITCHTIP}/x>; rel="previous"; results="false", <{GLITCHTIP}/x>; rel="next"; results="true"; cursor="c2"'
LAST = f'<{GLITCHTIP}/x>; rel="previous"; results="true"; cursor="c1", <{GLITCHTIP}/x>; rel="next"; results="false"'


TWO_PAGES = {
    "": ([issue(1, "prod-app"), issue(2, "staging-app")], FIRST),
    "c2": ([issue(3, "prod-web"), issue(4, "slot1-app"), issue(5, "slot0-app"), issue(6, "factory")], LAST),
}


@dataclasses.dataclass
class GlitchTips:
    """The pages the shared GlitchTip answers, and the URLs it was asked."""

    pages: dict[str, Any]
    asked: list[str]


@pytest.fixture
def glitchtips(world: World, monkeypatch: pytest.MonkeyPatch) -> GlitchTips:
    """The server is set up; at first its GlitchTip answers one page with no issue."""
    store.write_json(telemetry.server_json(world.ctx.places), SERVER)
    found = GlitchTips({"": ([], LAST)}, [])

    def http_get(url: str, headers: dict[str, str], timeout: float) -> Answer:
        assert headers["Authorization"] == "Bearer tok-read"
        found.asked.append(url)
        body, header = found.pages[parse_qs(urlsplit(url).query).get("cursor", [""])[0]]
        return Answer(200, {"link": header}, json.dumps(body).encode())

    monkeypatch.setattr(telemetry, "http_get", http_get)
    return found


def _next_poll(world: World) -> None:
    world.clock.t += world.ctx.settings().get_int("errors.poll_minutes") * 60
    errors_import.poll(world.ctx)


@pytest.fixture
def asked(world: World, glitchtips: GlitchTips) -> list[str]:
    """Each environment has read once with no issue open; now each GlitchTip answers two pages. The URLs asked since."""
    errors_import.poll(world.ctx)
    glitchtips.pages.update(TWO_PAGES)
    glitchtips.asked.clear()
    world.clock.t += world.ctx.settings().get_int("errors.poll_minutes") * 60
    return glitchtips.asked


def test_each_prod_and_slot_zero_issue_is_filed_once_across_polls(world: World, asked: list[str]) -> None:
    errors_import.poll(world.ctx)
    _next_poll(world)
    requests, broken = Queue.of(world.ctx).requests()
    assert not broken and len(asked) == 8  # both polls read both pages, for each environment
    assert sorted(r.where for r in requests) == [
        "prod-app:app.notes in show",
        "prod-web:app.notes in show",
        "slot0-app:app.notes in show",
    ]  # slot 1's issue, a staging-… project's and the factory's own are no environment's
    assert {(r.inbox, r.from_role, r.severity) for r in requests} == {("agents", "glitchtip", "normal")}
    assert {url.split("/api/0/organizations/notes/issues/?")[0] for url in asked} == {GLITCHTIP}


@pytest.mark.parametrize(
    ("level", "count", "severity"),
    [("error", "3", "normal"), ("fatal", "1", "severe"), ("error", "100", "severe"), (None, None, "normal")],
)
def test_a_fatal_or_frequent_issue_is_severe(level: str | None, count: str | None, severity: str) -> None:
    found = telemetry.Issue("1", "prod-app", "x", "y", level, None, None, count, None)
    assert errors_import.severity(found, 100) == severity


@pytest.mark.parametrize(
    ("env", "project", "reached"), [("prod", "prod-app", "promoted"), ("slot0", "slot0-app", "on-staging")]
)
def test_an_issue_seen_after_its_fix_reached_its_environment_is_filed_again(
    world: World, asked: list[str], env: str, project: str, reached: str
) -> None:
    errors_import.poll(world.ctx)
    queue = Queue.of(world.ctx)
    first = next(r for r in queue.requests()[0] if r.where.startswith(project))
    task = Task(id="t-0001", title="Notes show", requests=[first.id], goal_item="G1", severity="major",
                surfaces=["api"], status=reached, created=first.at, body="Fix it.", run=1, slices=1)  # fmt: skip
    with queue.lock():
        joined = dataclasses.replace(first, outcome="task t-0001")
        queue.write({task_rel(task.id): formats.dump_task(task), request_rel(first): formats.dump_request(joined)})
        queue.log(task.id, "2026-10-09T10:30:00Z", "warden", reached, f"on {env}", to=reached)

    _next_poll(world)  # the issue was last seen at 11:00, after the fix reached its environment at 10:30

    again = [r for r in Queue.of(world.ctx).requests()[0] if r.where.startswith(project)]
    assert len(again) == 2 and f"came back after its fix reached {env}" in again[-1].body


def test_the_first_read_takes_open_issues_as_known_and_files_one_only_once_seen_again(
    world: World, glitchtips: GlitchTips
) -> None:
    glitchtips.pages.update(TWO_PAGES)
    errors_import.poll(world.ctx)
    assert Queue.of(world.ctx).requests()[0] == []

    seen_again = {**issue(5, "slot0-app"), "lastSeen": "2026-10-09T12:00:00Z"}
    glitchtips.pages["c2"] = ([issue(3, "prod-web"), seen_again], LAST)
    _next_poll(world)

    filed = Queue.of(world.ctx).requests()[0]
    assert [(r.where, "is new" in r.body) for r in filed] == [("slot0-app:app.notes in show", True)]


def test_an_issue_holding_a_mark_the_product_plants_is_filed_once_as_noise(
    world: World, glitchtips: GlitchTips, asked: list[str]
) -> None:
    with (world.checkout / "factory.toml").open("a") as fh:
        fh.write('\n[planted]\nmarks = ["QaPlanted"]\n')
    planted = {**issue(7, "slot0-app"), "title": "QaPlanted: uncaught on /captures", "level": "fatal"}
    glitchtips.pages["c2"] = ([*TWO_PAGES["c2"][0], planted], LAST)
    errors_import.poll(world.ctx)
    assert "2 issues filed, 1 planted by QA as noise" in str(errors_import.note(world.ctx.places))
    _next_poll(world)

    outcomes = {r.body.split()[2]: r.outcome for r in Queue.of(world.ctx).requests()[0]}
    noise = "noise planted by QA: its title or culprit holds QaPlanted, which factory.toml's [planted] names"
    assert outcomes == {"1": "", "3": "", "5": "", "7": noise}
    assert world.events == []  # fatal, so severe, yet no one is woken for it


def _tester(sid: str, role: str, started: str, ended: str = "") -> Session:
    return Session(
        sid,
        f"orchestrator.{role}-{sid}",
        role,
        "qa",
        "m",
        "high",
        "",
        "s-orch",
        "/repo",
        "retired" if ended else "working",
        started,
        "s-orch",
        ended=ended,
        agent_id=sid,
    )


def test_a_staging_issue_names_the_regression_testers_walking_slot_0_when_it_was_first_seen(
    world: World, asked: list[str]
) -> None:
    testers = [
        _tester("a1", REGRESSION, "2026-10-09T09:58:00Z", "2026-10-09T10:03:00Z"),
        _tester("a2", REGRESSION, "2026-10-09T09:50:00Z", "2026-10-09T09:55:00Z"),
        _tester("a3", REGRESSION, "2026-10-09T09:59:00Z"),
        _tester("a4", "qa-ux", "2026-10-09T09:58:00Z"),  # a task's tester walks its implementer's slot
    ]
    registry.write(world.ctx.places, {t.id: t for t in testers})
    for name in ("checkout-pay", "cart"):
        doing.note_scenario(world.ctx.places, "a1", name)
    errors_import.poll(world.ctx)  # each issue was first seen at 10:00

    bodies = {r.body.split()[2]: r.body for r in Queue.of(world.ctx).requests()[0]}
    assert (
        "- Testers walking slot 0 when it was first seen: orchestrator.qa-regression-a1 from 2026-10-09T09:58:00Z to "
        "2026-10-09T10:03:00Z, having read checkout-pay, cart; orchestrator.qa-regression-a3 from "
        "2026-10-09T09:59:00Z, still walking, having read no scenario"
    ) in bodies["5"]
    assert "Testers walking" not in bodies["1"]


def test_an_import_that_cannot_read_factory_toml_fails_loudly_and_files_nothing(
    world: World, asked: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    (world.checkout / "factory.toml").unlink()
    with caplog.at_level(logging.WARNING):
        errors_import.poll(world.ctx)
    assert Queue.of(world.ctx).requests()[0] == []
    said = [("factory.toml is missing" in r.getMessage(), bool(r.exc_info)) for r in caplog.records]
    assert said == [(True, False)] * 2
