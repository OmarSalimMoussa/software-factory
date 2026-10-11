"""`factory errors` and `factory traces`: each workspace reads its own services and its own projects, and an answer
never outgrows telemetry.max_kib."""

from __future__ import annotations

import json
from typing import Any

import pytest

from runtime.shared import store
from runtime.shared.network import Answer
from runtime.warden.context import Context
from runtime.warden.health import telemetry

SERVER = {
    "glitchtip": {"url": "https://box.test:8200", "org": "notes", "token": "tok-server"},
    "langfuse": {"url": "https://box.test:3100", "public_key": "pk-lf-1", "secret_key": "sk-lf-1"},
}
LAPTOP = {"url": "http://127.0.0.1:8200", "org": "notes", "token": "tok-laptop"}


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


def test_a_slot_reads_the_laptops_glitchtip_and_only_its_own_projects(
    ctx: Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.write_json(telemetry.server_json(ctx.places), SERVER)
    store.write_json(telemetry.laptop_glitchtip_json(ctx.places), LAPTOP)
    asked = answering(monkeypatch, [issue(1, "slot3-app"), issue(2, "slot1-app"), issue(3, "prod-app")])
    answer = telemetry.errors(ctx, {"where": "slot", "slot": 3}, None)
    assert [row["id"] for row in answer["issues"]] == ["1"] and answer["where"] == "slot3"
    assert [(url.split("?")[0], auth) for url, auth in asked] == [
        ("http://127.0.0.1:8200/api/0/organizations/notes/issues/", "Bearer tok-laptop")
    ]


def test_an_answer_is_cut_to_max_kib_and_says_so(ctx: Context, monkeypatch: pytest.MonkeyPatch) -> None:
    store.write_json(telemetry.server_json(ctx.places), SERVER)
    answering(monkeypatch, [issue(n, "prod-app", title="x" * 8000) for n in range(20)])
    answer = telemetry.errors(ctx, {"where": "prod"}, None)
    limit = ctx.settings().get_int("telemetry.max_kib") * 1024
    assert len(json.dumps(answer).encode()) <= limit < 20 * 8000
    assert answer["trimmed"] is True and answer["issues"][0]["title"] == "x" * 8000
