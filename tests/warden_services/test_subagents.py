from __future__ import annotations

import pytest

from runtime.shared.models import WORKING, Session
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import registry, subagents
from runtime.warden.context import Caller
from runtime.warden.dispatch import Doors
from runtime.warden.verbs import IMPL


def _session(sid: str) -> Session:
    return Session(id=sid, name=f"impl-{sid}", role=IMPL, family="impl", model="m", effort="high", task="t-1",
                   parent="", worktree=f"/w/{sid}", slot=1, state=WORKING, started="2026-10-09T00:00:00+00:00",
                   started_by="orch", since="2026-10-09T00:00:00+00:00")  # fmt: skip


def _caller(session: str) -> Caller:
    return Caller(kind="agent", session=session, role=IMPL, uid=1000, pid=1)


def test_a_subagent_id_is_honoured_only_from_its_parent_and_never_once_retired(doors: Doors, places: Places) -> None:
    registry.write(places, {"s-a": _session("s-a"), "s-b": _session("s-b")})
    subagents.started(doors.context(_caller("s-a")), {"agent_id": "a1b2c3", "kind": "qa-ux"}, None)

    assert subagents.acting_as(places, _caller("s-a"), "a1b2c3").agent_id == "a1b2c3"
    with pytest.raises(Refused, match="not registered under your session"):
        subagents.acting_as(places, _caller("s-b"), "a1b2c3")

    subagents.ended(doors.context(_caller("s-a")), {"agent_id": "a1b2c3"}, None)
    with pytest.raises(Refused) as refused:
        subagents.acting_as(places, _caller("s-a"), "a1b2c3")
    assert refused.value.rule == "retired"
