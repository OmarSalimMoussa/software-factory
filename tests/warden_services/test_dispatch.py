from __future__ import annotations

import json
from typing import Any

import pytest
from warden_fakes import FakeClock, code_now

from runtime.shared import protocol, totp
from runtime.shared.models import WORKING, Session
from runtime.shared.protocol import Refused
from runtime.warden import verbs
from runtime.warden.context import Caller, Context
from runtime.warden.dispatch import AGENT_SOCKET, OWNER_SOCKET, Dispatcher, Doors
from runtime.warden.sessions import registry
from runtime.warden.verbs import ANY, DASH, IMPL, OWNER, Verb

DASHBOARD = Caller(kind="dashboard", role=DASH)
VERBS = {
    "settings.save": Verb("x:y", frozenset({DASH, OWNER}), coded=True, owner_socket=True),
    "factory.pause": Verb("x:y", frozenset({DASH, OWNER}), coded=True, owner_socket=True),
    "proposal.approve": Verb("x:y", frozenset({DASH, OWNER}), owner_socket=True),
    "status": Verb("x:y", ANY),
    "qa-ready": Verb("x:y", frozenset({IMPL})),
    "qa.result": Verb("x:y", frozenset({IMPL}), kinds=frozenset({"qa-ux"})),
}


class Handlers:
    def __init__(self) -> None:
        self.ran: list[str] = []
        self.refuse = False

    def table(self) -> dict[str, Any]:
        def handler(name: str) -> Any:
            def run(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
                if self.refuse:
                    raise Refused("settings", "caps.implementers must be from 1 to 6")
                self.ran.append(name)
                return {"done": name, "run": len(self.ran)}

            return run

        return {name: handler(name) for name in VERBS}


@pytest.fixture
def handlers() -> Handlers:
    return Handlers()


@pytest.fixture
def dispatcher(doors: Doors, handlers: Handlers) -> Dispatcher:
    return Dispatcher(doors, handlers.table(), VERBS)


def ask(dispatcher: Dispatcher, verb: str, caller: Caller = DASHBOARD, socket: str = OWNER_SOCKET,
        **args: Any) -> dict[str, Any]:  # fmt: skip
    return dispatcher.answer(caller, socket, protocol.request(verb, args))


def test_a_coded_action_runs_only_with_the_owners_code(
    dispatcher: Dispatcher, doors: Doors, handlers: Handlers
) -> None:
    assert ask(dispatcher, "settings.save")["refused"] == "code"
    assert ask(dispatcher, "settings.save", code=_wrong(doors))["refused"] == "code"
    assert handlers.ran == []

    assert ask(dispatcher, "settings.save", code=code_now(doors.places, doors.clock))["ok"] is True
    assert handlers.ran == ["settings.save"]


def test_only_signing_a_device_in_asks_for_the_code() -> None:
    assert sorted(name for name, verb in verbs.VERBS.items() if verb.coded) == ["device.admit"]


def test_a_code_is_accepted_once(dispatcher: Dispatcher, doors: Doors, handlers: Handlers) -> None:
    code = code_now(doors.places, doors.clock)
    assert ask(dispatcher, "settings.save", code=code)["ok"] is True

    again = ask(dispatcher, "factory.pause", code=code)

    assert again["refused"] == "code" and "already used" in again["reason"]
    assert handlers.ran == ["settings.save"]


def _wrong(doors: Doors) -> str:
    right = code_now(doors.places, doors.clock)
    secret = doors.places.totp_secret.read_text().strip()
    step = int(doors.clock.now() // totp.STEP_S)
    near = {totp.code_at(secret, step + d) for d in (-1, 0, 1)}
    return next(c for c in (f"{n:06d}" for n in range(1000)) if c not in near and c != right)


def test_wrong_codes_lock_coded_actions_for_a_while(dispatcher: Dispatcher, doors: Doors, handlers: Handlers) -> None:
    limit = doors.settings().get_int("dashboard.wrong_codes_to_lock")
    answers = [ask(dispatcher, "settings.save", code=_wrong(doors)) for _ in range(limit)]
    assert answers[0]["reason"] == f"wrong code, {limit - 1} tries left"
    assert answers[-1]["refused"] == "code-locked"

    assert ask(dispatcher, "settings.save", code=code_now(doors.places, doors.clock))["refused"] == "code-locked"
    assert handlers.ran == []

    assert isinstance(doors.clock, FakeClock)
    doors.clock.t += doors.settings().get_int("dashboard.lock_minutes") * 60 + totp.STEP_S
    assert ask(dispatcher, "settings.save", code=code_now(doors.places, doors.clock))["ok"] is True


def test_a_refused_action_is_not_a_wrong_code(dispatcher: Dispatcher, doors: Doors, handlers: Handlers) -> None:
    handlers.refuse = True
    refused = ask(dispatcher, "settings.save", code=code_now(doors.places, doors.clock))
    assert refused["refused"] == "settings"

    limit = doors.settings().get_int("dashboard.wrong_codes_to_lock")
    assert ask(dispatcher, "settings.save", code=_wrong(doors))["reason"] == f"wrong code, {limit - 1} tries left"


def test_an_idempotency_key_runs_its_action_once(dispatcher: Dispatcher, doors: Doors, handlers: Handlers) -> None:
    key = "k-0123456789"
    refused = ask(dispatcher, "settings.save", code=_wrong(doors), idempotency_key=key)
    assert refused["refused"] == "code"  # a refusal changed nothing, so the same key may run later

    first = ask(dispatcher, "settings.save", code=code_now(doors.places, doors.clock), idempotency_key=key)
    retry = ask(dispatcher, "settings.save", code=code_now(doors.places, doors.clock), idempotency_key=key)

    assert first == retry == {"done": "settings.save", "run": 1, "ok": True}
    assert handlers.ran == ["settings.save"]
    assert ask(dispatcher, "proposal.approve", idempotency_key=key)["refused"] == "idempotency"
    assert key in json.loads((doors.places.state / "idempotency.json").read_text())


@pytest.mark.parametrize(
    ("verb", "caller", "socket", "rule"),
    [
        ("status", Caller(kind="owner", role=OWNER), OWNER_SOCKET, "socket"),
        ("proposal.approve", Caller(kind="owner", role=OWNER), AGENT_SOCKET, "socket"),
        ("qa-ready", Caller(kind="agent", session="s1", role="designer"), AGENT_SOCKET, "role"),
        ("status", Caller(kind="agent"), AGENT_SOCKET, "role"),
        ("no-such-verb", Caller(kind="owner", role=OWNER), AGENT_SOCKET, "verb"),
    ],
)
def test_a_verb_answers_only_its_roles_on_its_socket(
    dispatcher: Dispatcher, handlers: Handlers, verb: str, caller: Caller, socket: str, rule: str
) -> None:
    assert ask(dispatcher, verb, caller, socket)["refused"] == rule
    assert handlers.ran == []


@pytest.mark.parametrize(
    ("verb", "kind", "allowed"),
    [("qa.result", "qa-ux", True), ("qa.result", "critic-ux", False), ("qa-ready", "qa-ux", False)],
    ids=["its-kind", "another-kind", "no-subagent-verb"],
)
def test_a_subagent_acts_in_its_parents_role_but_only_on_the_verbs_that_name_its_kind(
    dispatcher: Dispatcher, doors: Doors, handlers: Handlers, verb: str, kind: str, allowed: bool
) -> None:
    impl = Caller(kind="agent", session="s-impl", role=IMPL)
    sessions = {
        "s-impl": Session("s-impl", "impl-a", IMPL, "impl", "m", "high", "t-0001", "", "/w", WORKING,
                          "2026-10-09T12:00:00Z", "orchestrator"),
        "a1234567890abc": Session("a1234567890abc", "impl-a.tester", kind, "impl", "m", "high", "t-0001", "s-impl",
                                  "/w", WORKING, "2026-10-09T12:00:00Z", "s-impl"),
    }  # fmt: skip
    registry.write(doors.places, sessions)
    answer = dispatcher.answer(impl, AGENT_SOCKET, protocol.request(verb, {}, agent_id="a1234567890abc"))
    assert answer.get("ok", False) is allowed
    assert handlers.ran == ([verb] if allowed else [])
