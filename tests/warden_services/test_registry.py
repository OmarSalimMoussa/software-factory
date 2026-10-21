from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from runtime.shared import models
from runtime.shared.models import WAITING, WORKING, Session
from runtime.shared.paths import Places
from runtime.warden.sessions import registry


def _session(sid: str) -> Session:
    return Session(id=sid, name=f"impl-{sid}", role="implementer", family="impl", model="m", effort="high",
                   task="t-1", parent="", worktree=f"/w/{sid}", state=WORKING, started="2026-10-09T00:00:00+00:00",
                   started_by="orch", since="2026-10-09T00:00:00+00:00", kinds=["qa-ux"],
                   extensions={"qa": 1})  # fmt: skip


def test_each_read_hands_out_sessions_of_its_own_and_a_rewritten_registry_is_read_anew(places: Places) -> None:
    registry.write(places, {"s-a": _session("s-a")})
    mine = registry.read(places)["s-a"]
    mine.state = WAITING
    mine.kinds.append("qa-api")
    mine.extensions["qa"] = 2
    assert registry.read(places)["s-a"] == _session("s-a")

    registry.write(places, {"s-a": replace(_session("s-a"), state=WAITING)})
    assert registry.read(places)["s-a"].state == WAITING


def test_an_unchanged_registry_is_parsed_once_however_often_it_is_read(
    places: Places, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.write(places, {"s-a": _session("s-a"), "s-b": _session("s-b")})
    before = registry.read(places)
    real, parsed = models.load, []

    def counted(cls: type[Session], raw: dict[str, Any]) -> Session:
        parsed.append(raw["id"])
        return real(cls, raw)

    monkeypatch.setattr(models, "load", counted)
    assert [registry.read(places) for _ in range(3)] == [before] * 3
    assert parsed == []
    registry.write(places, {"s-a": _session("s-a")})
    assert list(registry.read(places)) == ["s-a"]
    assert parsed == ["s-a"]
