"""The owner's Claude accounts: a token never leaves the warden, the rules of add, use and remove, and a switch that
restarts the agents' daemon and brings each working session back without counting a crash."""

from __future__ import annotations

import json
import logging
import stat
from typing import Any, cast

import pytest
from lifecycle_fakes import World

from runtime.shared import claude_accounts, protocol
from runtime.shared.models import DEPRIORITIZED, RETIRED, WAITING, WITH_PARENT, WORKING
from runtime.shared.protocol import Refused
from runtime.system import layout
from runtime.warden import accounts, crashwatch, lifecycle, ops, permits, reflections
from runtime.warden.context import Caller
from runtime.warden.dispatch import OWNER_SOCKET, Dispatcher, Doors, resolve
from runtime.warden.verbs import DASH, VERBS

MAIN = "sk-ant-oat01-" + "Qm7" * 24 + "a1b2"
WORK = "sk-ant-oat01-" + "Vt4" * 24 + "c3d4"


def _dispatcher(world: World) -> Dispatcher:
    wanted = {
        name: verb for name, verb in VERBS.items() if name.startswith("account.") or name in ("op.get", "snapshot")
    }
    doors = Doors(world.places, lambda: world.settings, world.clock, cast(Any, world.launcher), cast(Any, None),
                  lambda kind, data: world.events.append((kind, data)), world.lock)  # fmt: skip
    return Dispatcher(doors, resolve(wanted), wanted)


def _ask(d: Dispatcher, verb: str, args: dict[str, Any] | None = None, data: bytes | None = None) -> dict[str, Any]:
    return d.answer(Caller(kind="dashboard", role=DASH), OWNER_SOCKET, protocol.request(verb, args or {}, data))


def _accounts(world: World) -> Dispatcher:
    d = _dispatcher(world)
    _ask(d, "account.add", {"name": "main"}, MAIN.encode())
    _ask(d, "account.add", {"name": "work"}, WORK.encode())
    return d


def _switch(world: World, name: str) -> dict[str, Any]:
    started = accounts.use(world.owner(), {"name": name}, None)
    ops.join(started["op"])
    op: dict[str, Any] = ops.get(world.owner(), {"id": started["op"]}, None)["op"]
    return op


def _secrets(world: World) -> Any:
    return layout.secrets(world.places.state)


def test_a_token_never_appears_in_an_answer_a_log_line_or_an_error(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    d = _dispatcher(world)
    answers = [
        _ask(d, "account.add", {"name": "main"}, MAIN.encode()),
        _ask(d, "account.add", {"name": "main"}, MAIN.encode()),
        _ask(d, "account.add", {"name": "Main"}, MAIN.encode()),
        _ask(d, "account.add", {"name": "spare"}, b"two words " + MAIN.encode()),
        _ask(d, "account.add", {"name": "spare"}, MAIN.encode() + b"\xff"),
        _ask(d, "account.add", {"name": "work"}, WORK.encode()),
        _ask(d, "account.list"),
        _ask(d, "snapshot", {"want": ["accounts"]}),
        _ask(d, "account.remove", {"name": "main"}),
    ]
    started = _ask(d, "account.use", {"name": "work"})
    ops.join(started["op"])
    answers += [started, _ask(d, "op.get", {"id": started["op"]}), _ask(d, "account.use", {"name": "work"})]
    answers += [_ask(d, "account.remove", {"name": "work"}), _ask(d, "account.remove", {"name": "main"})]

    said = json.dumps(answers) + caplog.text
    assert [a["ok"] for a in answers] == [True, False, False, False, False, True, True, True, False, True, True, True,
                                          False, True]  # fmt: skip
    for token in (MAIN, WORK):
        assert token[:-4] not in said
    assert answers[6]["accounts"][1]["last4"] == "c3d4"
    kept = [claude_accounts.token_file(_secrets(world), "work"), _secrets(world) / claude_accounts.UNIT_TOKEN]
    assert [(p.read_text(), stat.S_IMODE(p.stat().st_mode)) for p in kept] == [(WORK + "\n", 0o600)] * 2


@pytest.mark.parametrize(
    ("verb", "args", "data", "expect"),
    [
        ("account.list", {}, None, [("main", True), ("work", False)]),  # the first account added is the active one
        ("account.add", {"name": "spare"}, b"  sk-ant-oat01-a-third-token-e5f6\n", {"name": "spare", "active": False}),
        ("account.add", {"name": "work"}, b"sk-ant-oat01-a-third-token-e5f6", "name-taken"),
        ("account.add", {"name": "Work"}, b"sk-ant-oat01-a-third-token-e5f6", "args"),
        ("account.add", {"name": "spare\n"}, b"sk-ant-oat01-a-third-token-e5f6", "args"),  # it would name a file
        ("account.add", {"name": "-spare"}, b"sk-ant-oat01-a-third-token-e5f6", "args"),
        ("account.add", {"name": "s" * 32}, b"sk-ant-oat01-a-third-token-e5f6", "args"),
        ("account.add", {"name": "spare"}, b"sk-ant-oat01 a-third-token-e5f6", "token"),
        ("account.add", {"name": "spare"}, b"sk-ant-short", "token"),
        ("account.add", {"name": "spare"}, None, "token"),
        ("account.remove", {"name": "main"}, None, "active"),
        ("account.remove", {"name": "gone"}, None, "account"),
        ("account.use", {"name": "gone"}, None, "account"),
        ("account.use", {"name": "main"}, None, {"message": "The agents already use main; nothing changed."}),
    ],
)
def test_the_rules_of_add_use_and_remove(world: World, verb: str, args: dict[str, Any], data: bytes | None,
                                         expect: Any) -> None:  # fmt: skip
    d = _accounts(world)
    answer = _ask(d, verb, args, data)
    if isinstance(expect, str):
        assert (answer["ok"], answer["refused"]) == (False, expect)
    elif isinstance(expect, list):
        assert [(row["name"], row["active"]) for row in answer["accounts"]] == expect
    else:
        assert answer["ok"] and "op" not in answer and expect.items() <= answer.items()
    files = [claude_accounts.name_of(p) for p in claude_accounts.token_files(_secrets(world))]
    assert files == (["main", "spare", "work"] if answer["ok"] and verb == "account.add" else ["main", "work"])
    assert (_secrets(world) / claude_accounts.UNIT_TOKEN).read_text() == MAIN + "\n"


def test_a_switch_restarts_the_daemon_and_brings_each_working_session_back_without_a_crash(world: World) -> None:
    _accounts(world)
    world.queue.add("t-0001", status="building")
    world.queue.add("t-0002", status="designing")
    world.approve("t-0004")
    world.seat("5e550001", task="t-0001", started="2026-10-09T10:00:00Z")
    world.seat("0c0c0c0c", role="orchestrator", family="orch", started="2026-10-09T11:00:00Z")
    world.seat("5e550002", role="designer", family="design", task="t-0002", state=WAITING)
    world.seat("a9a9a9a9", role="qa-ux", family="qa", task="t-0001", parent="5e550001")
    world.seat("5e550003", task="t-0003", state=DEPRIORITIZED)
    for sid in ("5e550001", "0c0c0c0c", "5e550002"):
        world.run(sid)
    during: dict[str, str] = {}

    def meanwhile() -> None:
        """A tick of the session watch, a start and a second switch, while the daemon restarts."""
        crashwatch.watch_sessions(world.ctx())
        for name, act in (("start", lambda: lifecycle.start(world.ctx(), {"role": "implementer", "task": "t-0004"},
                                                            None)),
                          ("switch", lambda: accounts.use(world.owner(), {"name": "main"}, None))):  # fmt: skip
            try:
                act()
            except Refused as exc:
                during[name] = exc.rule

    world.launcher.on_restart = meanwhile
    op = _switch(world, "work")

    assert [(s["text"], s["state"]) for s in op["steps"]] == [
        ("Making work the active account", "done"),
        ("Restarting the agents' Claude daemon", "done"),
        ("Bringing back the sessions", "done"),
    ]
    assert (op["state"], world.launcher.restarts, during) == ("done", 1, {"start": "switching", "switch": "switching"})
    resumes = [(argv[2], argv[-1]) for argv in world.launcher.commands if argv[:2] == ["claude", "--resume"]]
    assert resumes == [(sid, accounts.NOTE) for sid in ("0c0c0c0c", "5e550001", "5e550002")]
    assert world.events == [] and reflections.entries(world.places) == []
    assert [world.session(sid).resume_tries for sid in ("0c0c0c0c", "5e550001", "5e550002")] == [0, 0, 0]
    tester, benched = world.session("a9a9a9a9"), world.session("5e550003")
    assert (tester.state, tester.retired, benched.state) == (RETIRED, WITH_PARENT, DEPRIORITIZED)
    assert (claude_accounts.folder(_secrets(world)) / claude_accounts.ACTIVE).read_text() == "work\n"
    assert (_secrets(world) / claude_accounts.UNIT_TOKEN).read_text() == WORK + "\n"
    assert permits.switching(world.ctx()) is None


def test_a_session_that_cannot_come_back_says_why_and_the_crash_watch_resumes_it_after(world: World) -> None:
    _accounts(world)
    world.queue.add("t-0001", status="building")
    world.seat("5e550001", task="t-0001")
    world.run("5e550001")
    world.launcher.copy = True  # Claude Code starts a copy instead of the session

    op = _switch(world, "work")

    assert (op["state"], op["steps"][2]["state"]) == ("failed", "failed")
    assert op["steps"][2]["detail"].startswith("implementer-5e550001 did not come back (Claude Code started a copy")
    world.launcher.copy = False
    crashwatch.watch(world.ctx())
    assert (world.session("5e550001").state, world.session("5e550001").resume_tries) == (WORKING, 1)
