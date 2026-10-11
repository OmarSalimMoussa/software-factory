"""Prod's unresolved GlitchTip issues become requests from glitchtip: each issue once, and only prod's."""

from __future__ import annotations

import dataclasses
import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from loop_fakes import World

from runtime.queue import formats
from runtime.queue.book import Queue, request_rel, task_rel
from runtime.shared import store
from runtime.shared.models import Task
from runtime.warden import errors_import, telemetry
from runtime.warden.telemetry import Reply

GLITCHTIP = "https://box.test:8200"
SERVER = {
    "glitchtip": {"url": GLITCHTIP, "org": "notes", "token": "tok-read"},
    "langfuse": {"url": "https://box.test:3100", "public_key": "pk-lf-1", "secret_key": "sk-lf-1"},
}


def issue(n: int, project: str) -> dict[str, Any]:
    return {"id": str(n), "title": f"KeyError: 'note' ({n})", "culprit": "app.notes in show", "level": "error",
            "firstSeen": "2026-10-09T10:00:00Z", "lastSeen": "2026-10-09T11:00:00Z", "count": "3",
            "permalink": f"{GLITCHTIP}/{project}/issues/{n}",
            "project": {"id": "1", "slug": project, "name": project}}  # fmt: skip


# GlitchTip's Link header: the first page names a next one; the last names only a previous one, never followed.
FIRST = f'<{GLITCHTIP}/x>; rel="previous"; results="false", <{GLITCHTIP}/x>; rel="next"; results="true"; cursor="c2"'
LAST = f'<{GLITCHTIP}/x>; rel="previous"; results="true"; cursor="c1", <{GLITCHTIP}/x>; rel="next"; results="false"'


@pytest.fixture
def asked(world: World, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The server set-up ran and the product's queue is in place; GlitchTip answers two pages. The URLs it was asked."""
    store.write_json(telemetry.server_json(world.ctx.places), SERVER)
    pages = {
        "": ([issue(1, "prod-app"), issue(2, "staging-app")], FIRST),
        "c2": ([issue(3, "prod-web"), issue(4, "slot1-app")], LAST),
    }
    found: list[str] = []

    def http_get(url: str, headers: dict[str, str], timeout: float) -> Reply:
        assert headers["Authorization"] == "Bearer tok-read"
        found.append(url)
        body, header = pages[parse_qs(urlsplit(url).query).get("cursor", [""])[0]]
        return Reply(200, {"link": header}, json.dumps(body).encode())

    monkeypatch.setattr(telemetry, "http_get", http_get)
    return found


def test_each_prod_and_staging_issue_is_filed_once_across_polls(world: World, asked: list[str]) -> None:
    errors_import.poll(world.ctx)
    world.clock.t += world.ctx.settings().get_int("errors.poll_minutes") * 60
    errors_import.poll(world.ctx)
    requests, broken = Queue.of(world.ctx).requests()
    assert not broken and len(asked) == 8  # both polls read both pages, for each environment
    assert sorted(r.where for r in requests) == [
        "prod-app:app.notes in show",
        "prod-web:app.notes in show",
        "staging-app:app.notes in show",
    ]  # a slot's issue is no environment's
    assert {(r.inbox, r.from_role, r.severity) for r in requests} == {("agents", "glitchtip", "normal")}
    assert all(url.startswith(f"{GLITCHTIP}/api/0/organizations/notes/issues/?") for url in asked)


@pytest.mark.parametrize(
    ("level", "count", "severity"),
    [("error", "3", "normal"), ("fatal", "1", "severe"), ("error", "100", "severe"), (None, None, "normal")],
)
def test_a_fatal_or_frequent_issue_is_severe(level: str | None, count: str | None, severity: str) -> None:
    found = telemetry.Issue("1", "prod-app", "x", "y", level, None, None, count, None)
    assert errors_import.severity(found, 100) == severity


def test_an_issue_seen_after_its_fix_reached_prod_is_filed_again(world: World, asked: list[str]) -> None:
    errors_import.poll(world.ctx)
    queue = Queue.of(world.ctx)
    first = next(r for r in queue.requests()[0] if r.where.startswith("prod-app"))
    task = Task(id="t-0001", title="Notes show", requests=[first.id], goal_item="G1", severity="major",
                surfaces=["api"], status="promoted", created=first.at, body="Fix it.", run=1, slices=1)  # fmt: skip
    with queue.lock():
        joined = dataclasses.replace(first, outcome="task t-0001")
        queue.write({task_rel(task.id): formats.dump_task(task), request_rel(first): formats.dump_request(joined)})
        queue.log(task.id, "2026-10-09T10:30:00Z", "warden", "promoted", "on prod", to="promoted")

    world.clock.t += world.ctx.settings().get_int("errors.poll_minutes") * 60
    errors_import.poll(world.ctx)  # the issue was last seen at 11:00, after the fix reached prod at 10:30

    again = [r for r in Queue.of(world.ctx).requests()[0] if r.where.startswith("prod-app")]
    assert len(again) == 2 and "came back after its fix shipped" in again[-1].body
