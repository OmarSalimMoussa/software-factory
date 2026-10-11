"""The `factory` command: it reads the caller's files itself and sends bytes, never a path or stdin, and every
command line asks for the verb the guard reads from it."""

from __future__ import annotations

import io
import json

import pytest

from runtime.cli import accounts, factory
from runtime.cli.factory import Call, CliError, build
from runtime.guard.factory_calls import verb_of
from runtime.warden.verbs import VERBS

FILE = "/home/agent/wt/picture.svg"
CONTENT = b"<svg/>"


def _build(argv: list[str]) -> Call:
    def read_file(path: str) -> bytes:
        assert path == FILE
        return CONTENT

    return build(argv, read_file)


@pytest.mark.parametrize(
    "argv",
    [
        ["propose", "t-0001", "--picture", FILE, "--why", "first visit", "--run", "2", "--reason", "shaky cause"],
        ["needs-owner", "t-0001", "--picture", FILE, "--brief", "https://claude.ai/code/artifact/x", "--ask", "Go?"],
        ["needs-owner", "t-0001", "--picture", FILE, "--question", "Keep the old link?"],
        ["spec", "save", "t-0001", FILE, "--slices", "3"],
        ["request", "new", "--severity", "normal", "--where", "the sign-in page", "--body", FILE],
        ["triage", "apply", "--body", FILE],
        ["queue", "check", "abc1234", "--body", FILE],
        ["queue", "task", "new", "--requests", "r-2026-10-09-0001", "--title", "T", "--body", FILE],
    ],
)
def test_a_named_file_is_sent_as_its_bytes_never_as_its_path(argv: list[str]) -> None:
    call = _build(argv)
    assert call.data == CONTENT
    assert FILE not in json.dumps(call.args) and not {"picture", "body"} & set(call.args)


@pytest.mark.parametrize(
    "argv",
    [
        ["start", "implementer", "t-0001"],
        ["stop", "impl-t-0001"],
        ["resume", "impl-t-0001", "go on"],
        ["extend", "impl-t-0001", "qa"],
        ["doing", "fixing the cookie"],
        ["tick"],
        ["inbox"],
        ["inbox", "--wait"],
        ["push"],
        ["promote", "--back"],
        ["reflect", "--kind", "friction", "a rule slowed me"],
        ["status"],
        ["settings", "show"],
        ["errors"],
        ["errors", "--where", "slot", "3", "--issue", "12"],
        ["traces", "--where", "staging", "--trace", "abc"],
        ["request", "new", "--from", "owner", "--severity", "normal", "--where", "the sign-in page", "--body", FILE],
        ["triage", "apply", "--body", FILE],
        ["queue", "next"],
        ["queue", "check", "abc1234", "--body", FILE],
        ["queue", "task", "new", "--body", FILE, "--requests", "r-2026-10-09-0001", "--title", "T", "--goal-item", "G1",
         "--severity", "major", "--surfaces", "ui,api", "--cause", "x.py:1", "--slices", "1", "--owner-class", "none"],
        ["queue", "task", "facts", "t-0001", "--surfaces", "ui", "api", "--slices", "2"],
        ["queue", "task", "status", "t-0001", "turn"],
        ["route", "t-0001"],
        ["propose", "t-0001", "--picture", FILE, "--why", "now"],
        ["propose", "--none"],
        ["spec", "save", "t-0001", FILE, "--slices", "3"],
        ["card", "comments", "t-0001", "3"],
        ["needs-owner", "t-0001", "--picture", FILE, "--question", "Why?"],
        ["turn", "next"],
        ["slot", "claim"],
        ["slot", "up", "web"],
        ["slot", "free", "2"],
        ["slot", "sign-in", "tester-1@example.com"],
        ["slot", "seed", "load", "basic", "--as", "tester-1@example.com"],
        ["slot", "desktop", "up", "tester-1@example.com"],
        ["qa", "plan"],
        ["qa", "result", "--head", "abc1234", "--blockers", "0", "--majors", "1", "--minors", "2"],
        ["qa-ready", "abc1234", "--slices", "abc1234", "def5678"],
        ["qa-clean"],
        ["ready-check"],
    ],
)  # fmt: skip
def test_every_command_asks_for_a_verb_the_warden_and_the_guard_know(argv: list[str]) -> None:
    call = _build(argv)
    assert call.verb in VERBS
    assert verb_of(["factory", *argv]) == call.verb


def test_task_facts_arrive_typed() -> None:
    call = _build(["queue", "task", "facts", "t-0001", "--surfaces", "ui,", "api", "--slices", "2", "--after", "t-2"])
    assert call.args == {"task": "t-0001", "surfaces": ["ui", "api"], "slices": 2, "after": ["t-2"]}


def test_propose_none_names_no_task() -> None:
    with pytest.raises(CliError):
        _build(["propose", "t-0001", "--none"])


def test_the_subagent_id_the_guard_puts_first_travels_with_the_request() -> None:
    call = _build(["--agent-id", "a1b2c3", "qa", "result", "--head", "abc1234", "--blockers", "0", "--majors", "0",
                   "--minors", "0"])  # fmt: skip
    assert (call.verb, call.agent_id) == ("qa.result", "a1b2c3")
    assert _build(["qa-clean"]).agent_id == ""
    with pytest.raises(CliError):
        _build(["--agent-id", "--head"])


def test_a_refusal_is_printed_with_its_rule_and_fails() -> None:
    out, err = io.StringIO(), io.StringIO()
    assert factory.show({"ok": False, "refused": "max-proposals", "reason": "wait"}, out, err) == 1
    assert err.getvalue() == "factory: refused (max-proposals): wait\n"


def test_the_owners_account_commands_send_the_token_as_bytes_and_follow_a_switch_to_its_end() -> None:
    token = "sk-ant-oat01-from-a-pipe-a1b2"
    steps = [{"text": "Making work the active account", "state": "done", "detail": "its token ends in a1b2"},
             {"text": "Restarting the agents' Claude daemon", "state": "running", "detail": ""}]  # fmt: skip
    answers = iter([
        {"ok": True, "name": "work", "active": False, "message": "Added work."},
        {"ok": True, "name": "work", "op": "op-1", "message": "Switching the agents to work."},
        {"ok": True, "op": {"state": "running", "steps": steps}},
        {"ok": True, "op": {"state": "done", "steps": [steps[0], {**steps[1], "state": "done"}]}},
    ])  # fmt: skip
    calls: list[Call] = []

    def ask(call: Call) -> dict[str, object]:
        calls.append(call)
        return next(answers)

    out, err = io.StringIO(), io.StringIO()
    piped = io.StringIO(token + "\n")
    assert accounts.run(["add", "work"], ask, piped, out, err) == 0
    assert accounts.run(["use", "work"], ask, io.StringIO(), out, err, sleep=lambda s: None) == 0

    assert [(c.verb, c.args, c.data) for c in calls[:2]] == [
        ("account.add", {"name": "work"}, (token + "\n").encode()),
        ("account.use", {"name": "work"}, None),
    ]
    assert token[:-4] not in out.getvalue() + err.getvalue()
    assert out.getvalue().splitlines() == [
        "Added work.",
        "Switching the agents to work.",
        "  done   Making work the active account: its token ends in a1b2",
        "  ...    Restarting the agents' Claude daemon",
        "  done   Restarting the agents' Claude daemon",
    ]
    for argv in (["factory", "account", "use", "work"], ["factory", "account", "add", "work"]):
        assert VERBS[verb_of(argv)].owner_socket  # the guard refuses every owner verb to a session
