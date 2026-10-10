"""Every page the server builds from real state has the shape the UI's fixtures have: the same keys, the same kinds
of values, at every depth. Unknown values (None) may stand anywhere."""

from __future__ import annotations

import json
from typing import Any

import pytest
from dashboard_rig import Rig
from dashboard_seed import FIXTURES

from runtime.dashboard.views.settings import LABELS
from runtime.shared import store
from runtime.shared.settings import SCHEMA

BROKEN = {"broken", "id", "error"}


def _pick(mine: dict[str, Any], candidates: list[Any]) -> Any:
    """The fixture row to compare a row with: the one it matches best (rows differ by kind, control or origin)."""
    return min(candidates, key=lambda c: len(shape(mine, c)))


def shape(mine: Any, theirs: Any, where: str = "") -> list[str]:
    if mine is None or theirs is None:
        return []
    if isinstance(theirs, bool) or isinstance(mine, bool):
        return [] if isinstance(mine, bool) and isinstance(theirs, bool) else [f"{where}: {mine!r} vs {theirs!r}"]
    if isinstance(theirs, int | float):
        return [] if isinstance(mine, int | float) else [f"{where}: want a number, got {mine!r}"]
    if isinstance(theirs, str):
        return [] if isinstance(mine, str) else [f"{where}: want text, got {mine!r}"]
    if isinstance(theirs, list):
        if not isinstance(mine, list):
            return [f"{where}: want a list"]
        candidates = [t for t in theirs if not (isinstance(t, dict) and t.get("broken"))]
        if len({type(t) for t in candidates}) > 1:  # a tuple, such as ["tasks", "Tasks", 14]: position by position
            return [e for i, (m, t) in enumerate(zip(mine, theirs, strict=False)) for e in shape(m, t, f"{where}[{i}]")]
        errors: list[str] = []
        for i, row in enumerate(mine):
            if isinstance(row, dict) and row.get("broken"):
                errors += [] if set(row) == BROKEN else [f"{where}[{i}]: a broken row is {sorted(BROKEN)}"]
            elif candidates:
                errors += shape(
                    row, _pick(row, candidates) if isinstance(row, dict) else candidates[0], f"{where}[{i}]"
                )
        return errors
    if not isinstance(mine, dict):
        return [f"{where}: want an object"]
    want = {k for k in theirs if not k.startswith("_")}
    if set(mine) != want:
        return [f"{where}: missing {sorted(want - set(mine))}, extra {sorted(set(mine) - want)}"]
    return [e for k in want for e in shape(mine[k], theirs[k], f"{where}.{k}")]


def _fixture(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    if name.startswith("agent/") and "agent" in raw:  # what the panel gained after the fixtures were drawn
        raw["agent"].update(thread=[], message=None, chat_note=None)
    if name == "settings.json":  # a row's control decides its value's kind, whatever its group
        every = [row for group in raw["settings"]["groups"] for row in group["rows"]]
        raw["settings"]["groups"] = [{**g, "rows": every} for g in raw["settings"]["groups"]]
    return raw


PAGES = [
    ("/api/shell", ["shell.json"]),
    ("/api/home", ["home.json"]),
    ("/api/work?tab=tasks", ["work-tasks.json"]),
    ("/api/work?tab=requests", ["work-requests.json"]),
    ("/api/work?tab=parked", ["work-parked.json"]),
    ("/api/work?tab=checks", ["work-checks.json"]),
    ("/api/task/t-0017", ["task/t-0017.json"]),
    ("/api/task/t-0027", ["task/t-0027.json"]),
    ("/api/task/t-0013", ["task/t-0013.json"]),
    ("/api/agents", ["agents.json"]),
    ("/api/agent/orchestrator", ["agent/orchestrator.json"]),
    ("/api/agent/impl-project-names", ["agent/impl-project-names.json"]),
    ("/api/agent/impl-sign-in-loop/stop-preview", ["agent/impl-sign-in-loop-stop-preview.json"]),
    ("/api/history/h1", ["history/h1.json"]),
    ("/api/spend?range=14&slice=family", ["spend.json", "spend-range-14.json", "spend-slice-family.json"]),
    ("/api/spend?range=7&slice=agent", ["spend-range-7.json", "spend-slice-agent.json"]),
    ("/api/spend?range=1&slice=model", ["spend-range-1.json", "spend-slice-model.json"]),
    ("/api/spend?range=14&slice=task", ["spend-slice-task.json"]),
    ("/api/doc/t-0017", ["doc/t-0017.json"]),
    ("/api/doc/s-0012", ["doc/s-0012.json"]),
    ("/api/doc/GOAL.md", ["doc/GOAL.md.json"]),
    ("/api/doc/c-1009-1405", ["doc/c-1009-1405.json"]),
    ("/api/doc/sr1", ["doc/sr1.json"]),
    ("/api/settings", ["settings.json"]),
    ("/api/reflect", ["reflect.json"]),
    ("/api/guard", ["guard.json"]),
    ("/api/push", ["push.json"]),
]


@pytest.mark.parametrize(("url", "fixtures"), PAGES)
def test_every_page_has_the_fixture_shapes(rig: Rig, url: str, fixtures: list[str]) -> None:
    parts = rig.parts(url)
    failed = {name: part["error"] for name, part in parts.items() if not part["ok"]}
    assert not failed
    for name in fixtures:
        for part, theirs in _fixture(name).items():
            assert part in parts, f"{url} has no {part}"
            assert shape(parts[part]["data"], theirs, part) == []


def test_the_outage_band_has_the_fixture_shape(rig: Rig) -> None:
    store.write_json(
        rig.places.state / "outage.json",
        {
            "since": "2026-10-09T11:30:00Z",
            "stage": "fixing",
            "task": "t-0021",
            "session": "impl-staging-down",
            "bypass_used": True,
        },
    )
    band = rig.parts("/api/home")["outage"]["data"]
    assert shape(band, _fixture("home@outage.json")["outage"], "outage") == []
    assert band["actions"] == ["watch", "stop"] and "<b>Staging is down after the last deploy</b>" in band["message"]


def test_settings_cover_every_key_of_the_schema(rig: Rig) -> None:
    rows = [r["key"] for g in rig.parts("/api/settings")["settings"]["data"]["groups"] for r in g["rows"]]
    assert sorted(rows) == sorted(SCHEMA) == sorted(LABELS)


def test_a_file_that_cannot_be_read_fails_only_its_own_part(rig: Rig) -> None:
    rig.places.cards.write_text("{not json")
    parts = rig.parts("/api/home")
    assert not parts["needs"]["ok"] and "could not read" in parts["needs"]["error"]
    assert all(part["ok"] for name, part in parts.items() if name != "needs")


def test_a_history_line_that_cannot_be_read_is_one_failed_row(rig: Rig) -> None:
    rows = rig.parts("/api/agents")["history"]["data"]["rows"]
    failed = [row for row in rows if row.get("broken")]
    assert failed == [{"broken": True, "id": "history line 6", "error": "line 6 of history.jsonl cannot be read"}]
    assert len(rows) == 25 and all(row.get("why") for row in rows if not row.get("broken"))
