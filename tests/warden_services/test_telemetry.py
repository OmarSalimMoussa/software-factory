"""`factory errors` and `factory logs`: each workspace reads its own projects of the shared GlitchTip, and an answer
never outgrows telemetry.max_kib."""

from __future__ import annotations

import json
from typing import Any

import pytest

from runtime.shared import store
from runtime.shared.network import Answer
from runtime.shared.protocol import Refused
from runtime.warden.context import Context
from runtime.warden.health import logs, telemetry

SERVER = {"glitchtip": {"url": "https://box.test:8200", "org": "notes", "token": "tok-server"}}


def issue(n: int, project: str, title: str = "KeyError") -> dict[str, Any]:
    return {"id": str(n), "title": title, "culprit": "app.notes in show", "level": "error", "count": "1",
            "firstSeen": "2026-10-09T10:00:00Z", "lastSeen": "2026-10-09T11:00:00Z",
            "project": {"id": "1", "slug": project, "name": project}}  # fmt: skip


def answering(monkeypatch: pytest.MonkeyPatch, body: Any) -> list[tuple[str, str]]:
    """GlitchTip answers ``body`` on one page; the URLs and credentials it was asked with."""
    asked: list[tuple[str, str]] = []

    def http_get(url: str, headers: dict[str, str], timeout: float) -> Answer:
        asked.append((url, headers["Authorization"]))
        return Answer(200, {}, json.dumps(body).encode())

    monkeypatch.setattr(telemetry, "http_get", http_get)
    return asked


def test_a_slot_reads_the_shared_glitchtip_and_only_its_own_projects(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.write_json(telemetry.server_json(ctx.places), SERVER)
    found = [issue(1, "slot3-app"), issue(2, "slot1-app"), issue(3, "prod-app"), issue(4, "factory")]
    asked = answering(monkeypatch, found)
    answer = telemetry.errors(ctx, {"where": "slot", "slot": 3}, None)
    assert [row["id"] for row in answer["issues"]] == ["1"] and answer["where"] == "slot3"
    assert [(url.split("?")[0], auth) for url, auth in asked] == [
        ("https://box.test:8200/api/0/organizations/notes/issues/", "Bearer tok-server")
    ]


def test_an_answer_is_cut_to_max_kib_and_says_so(ctx: Context, monkeypatch: pytest.MonkeyPatch) -> None:
    store.write_json(telemetry.server_json(ctx.places), SERVER)
    answering(monkeypatch, [issue(n, "prod-app", title="x" * 8000) for n in range(20)])
    answer = telemetry.errors(ctx, {"where": "prod"}, None)
    limit = ctx.settings().get_int("telemetry.max_kib") * 1024
    assert len(json.dumps(answer).encode()) <= limit < 20 * 8000
    assert answer["trimmed"] is True and answer["issues"][0]["title"] == "x" * 8000


def serving(monkeypatch: pytest.MonkeyPatch, pages: dict[str, tuple[Any, str]]) -> list[str]:
    """GlitchTip answers each address in ``pages`` with its body and Link header; the addresses it was asked."""
    asked: list[str] = []

    def http_get(url: str, headers: dict[str, str], timeout: float) -> Answer:
        asked.append(url)
        body, link = pages[url.removeprefix("https://box.test:8200")]
        return Answer(200, {"link": link} if link else {}, json.dumps(body).encode())

    monkeypatch.setattr(telemetry, "http_get", http_get)
    return asked


def next_page(cursor: str) -> str:
    return f'<https://box.test:8200/x?cursor={cursor}>; rel="next"; results="true"; cursor="{cursor}"'


EVENT = {"title": "boom", "sdk": {"name": "sentry.python"}, "tags": [{"key": "capture", "value": "c-1"}], "entries": [
    {"type": "request", "data": {"method": "GET", "url": "http://app:8000/a", "headers": None, "query": None}},
]}  # fmt: skip


def test_an_issue_answers_its_newest_event_with_its_request_section_or_whole(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.write_json(telemetry.server_json(ctx.places), SERVER)
    serving(monkeypatch, {"/api/0/issues/45/": (issue(45, "slot0-app"), ""),
                          "/api/0/issues/45/events/latest/": (EVENT, "")})  # fmt: skip
    asked = {"where": "slot", "slot": 0, "issue": "45"}
    summary = telemetry.errors(ctx, asked, None)["latest_event"]
    assert summary["request"] == EVENT["entries"][0]["data"] and summary["tags"] == {"capture": "c-1"}
    assert telemetry.errors(ctx, {**asked, "whole": True}, None)["latest_event"] == EVENT
    with pytest.raises(Refused, match="--issue"):
        telemetry.errors(ctx, {"where": "slot", "slot": 0, "whole": True}, None)


ORG = "/api/0/organizations/notes"
ISSUES = f"{ORG}/issues/?query=is%3Aunresolved&sort=-last_seen"


def seen(n: int, project: str, title: str, culprit: str, at: str) -> dict[str, Any]:
    return {**issue(n, project, title), "culprit": culprit, "lastSeen": f"2026-10-09T{at}:00Z"}


NEWEST = [seen(1, "prod-app", "NoPicture: a visit got no picture", "app.visits in watch", "12:00"),
          seen(2, "prod-app", "KeyError", "app.notes in show", "11:00"),
          seen(3, "slot0-app", "NoPicture on staging", "app.visits in watch", "10:00")]  # fmt: skip
OLDER = [seen(4, "prod-web", "TimeoutError", "web.nopicture in load", "09:00")]


@pytest.mark.parametrize(
    ("args", "found", "pages"),
    [
        ({"find": "nopicture"}, ["1", "4"], 2),  # in the title or the culprit, in any case
        ({"since": "2026-10-09T10:30:00Z"}, ["1", "2"], 1),  # newest first: the first one seen before ends the read
        ({"find": "KEYERROR", "since": "2026-10-09T10:30:00Z"}, ["2"], 1),
        ({"find": "no such words"}, [], 2),
        ({"since": "10:30"}, "args", 0),
        ({"find": " "}, "args", 0),
        ({"find": "nopicture", "issue": "45"}, "args", 0),
    ],
)
def test_errors_are_found_by_their_text_and_the_time_they_were_last_seen(
    ctx: Context, monkeypatch: pytest.MonkeyPatch, args: dict[str, Any], found: list[str] | str, pages: int
) -> None:
    store.write_json(telemetry.server_json(ctx.places), SERVER)
    asked = serving(monkeypatch, {ISSUES: (NEWEST, next_page("p2")), f"{ISSUES}&cursor=p2": (OLDER, "")})
    if isinstance(found, str):
        with pytest.raises(Refused) as refused:
            telemetry.errors(ctx, {"where": "prod", **args}, None)
        assert refused.value.rule == found and asked == []
        return
    answer = telemetry.errors(ctx, {"where": "prod", **args}, None)
    assert [row["id"] for row in answer["issues"]] == found and len(asked) == pages


LINE ={"timestamp": "2026-10-17T09:17:27Z", "level": "warn", "body": "store failure", "data": {"logger.name": "app"},
        "projectId": 37}  # fmt: skip


def test_logs_are_the_workspaces_own_projects_lines_found_by_glitchtips_search(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.write_json(telemetry.server_json(ctx.places), SERVER)
    lines = f"{ORG}/logs/?project=30&project=37&query=sentinel&start=2026-10-17T09%3A17%3A00%2B00%3A00&limit=20"
    asked = serving(monkeypatch, {
        f"{ORG}/projects/": ([{"id": "30", "slug": "slot0-api"}, {"id": "16", "slug": "prod-api"}], next_page("p2")),
        f"{ORG}/projects/?cursor=p2": ([{"id": 37, "slug": "slot0-app"}, {"id": "44", "slug": "slot1-app"}], ""),
        lines: ([LINE], next_page("l2")),
    })  # fmt: skip
    answer = logs.logs(ctx, {"where": "slot", "slot": 0, "find": "sentinel", "since": "2026-10-17T09:17:00Z"}, None)
    assert answer["lines"] == [{"at": LINE["timestamp"], "project": "slot0-app", "level": "warn",
                                "text": "store failure", "attributes": {"logger.name": "app"}}]  # fmt: skip
    assert answer["more"] is True and asked[-1] == f"https://box.test:8200{lines}"


@pytest.mark.parametrize(
    ("args", "projects", "page", "rule"),
    [
        ({}, [{"id": "16", "slug": "prod-api"}], None, None),  # no project of its own: GlitchTip is never asked
        ({}, [{"id": "30", "slug": "slot0-api"}], [{**LINE, "projectId": 16}], "telemetry"),  # a line not asked for
        ({"since": "09:17"}, [], None, "args"),
        ({"find": " "}, [], None, "args"),
    ],
)
def test_logs_never_stray_from_the_workspace_and_refuse_a_bad_search(
    ctx: Context, monkeypatch: pytest.MonkeyPatch, args: dict[str, Any], projects: list[Any], page: Any,
    rule: str | None,
) -> None:  # fmt: skip
    store.write_json(telemetry.server_json(ctx.places), SERVER)
    asked = serving(monkeypatch, {f"{ORG}/projects/": (projects, ""), f"{ORG}/logs/?project=30&limit=20": (page, "")})
    if rule is None:
        assert logs.logs(ctx, {"where": "slot", "slot": 0, **args}, None)["lines"] == []
        assert asked == [f"https://box.test:8200{ORG}/projects/"]
        return
    with pytest.raises(Refused) as refused:
        logs.logs(ctx, {"where": "slot", "slot": 0, **args}, None)
    assert refused.value.rule == rule
