from __future__ import annotations

import pytest

from runtime.shared.models import WORKING, Session
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden.context import Caller
from runtime.warden.dispatch import Doors
from runtime.warden.sessions import registry, subagents
from runtime.warden.verbs import IMPL, SESSIONS


def _session(sid: str) -> Session:
    return Session(id=sid, name=f"impl-{sid}", role=IMPL, family="impl", model="m", effort="high", task="t-1",
                   parent="", worktree=f"/w/{sid}", state=WORKING, started="2026-10-09T00:00:00+00:00",
                   started_by="orch", since="2026-10-09T00:00:00+00:00")  # fmt: skip


def _caller(session: str) -> Caller:
    return Caller(kind="agent", session=session, role=IMPL, uid=1000, pid=1)


def test_a_browser_lane_registers_as_its_kind_and_keeps_the_lane_it_started_as(doors: Doors, places: Places) -> None:
    registry.write(places, {"s-a": _session("s-a")})
    ctx = doors.context(_caller("s-a"))

    subagents.started(ctx, {"agent_id": "a1b2c3", "kind": "qa-ux-2"}, None)

    row = registry.read(places)["a1b2c3"]
    assert (row.role, row.started_as, row.model) == ("qa-ux", "qa-ux-2", ctx.settings().model("qa_ux")[0])


def test_a_subagent_id_is_honoured_only_from_its_parent_and_never_once_retired(doors: Doors, places: Places) -> None:
    registry.write(places, {"s-a": _session("s-a"), "s-b": _session("s-b")})
    subagents.started(doors.context(_caller("s-a")), {"agent_id": "a1b2c3", "kind": "qa-ux"}, None)

    assert subagents.acting_as(places, _caller("s-a"), "a1b2c3").agent_id == "a1b2c3"
    with pytest.raises(Refused, match="not registered under your session"):
        subagents.acting_as(places, _caller("s-b"), "a1b2c3")

    subagents.ended(doors.context(_caller("s-a")), {"agent_id": "a1b2c3", "ended": "finished"}, None)
    with pytest.raises(Refused) as refused:
        subagents.acting_as(places, _caller("s-a"), "a1b2c3")
    assert refused.value.rule == "retired"


@pytest.mark.parametrize("kind", sorted(SESSIONS))
def test_a_role_the_warden_starts_as_a_session_is_never_registered_as_a_subagent(
    doors: Doors, places: Places, kind: str
) -> None:
    registry.write(places, {"s-a": _session("s-a")})

    with pytest.raises(Refused) as refused:
        subagents.started(doors.context(_caller("s-a")), {"agent_id": "a1b2c3", "kind": kind}, None)

    assert refused.value.rule == "kind" and "a1b2c3" not in registry.read(places)
