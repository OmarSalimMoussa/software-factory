"""The `factory` command: it reads the caller's files itself and sends bytes, never a path or stdin, and every
command line asks for the verb the guard reads from it."""

from __future__ import annotations

import io
import json
import shlex
from pathlib import Path

import pytest

from runtime.cli import accounts, factory
from runtime.cli.factory import Call, CliError, build
from runtime.guard.factory_calls import verb_of
from runtime.shared.paths import Places
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
        ["needs-owner", "t-0001", "--picture", FILE, "--ask", "Go?"],
        ["brief", "save", "t-0001", FILE],
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
        ["promote", "--back"],
        ["reflect", "--kind", "friction", "a rule slowed me"],
        ["status"],
        ["settings", "show"],
        ["errors"],
        ["errors", "--where", "slot", "3", "--issue", "12"],
        ["errors", "--where", "slot", "3", "--issue", "12", "--whole"],
        ["logs", "--where", "slot", "0", "--find", "sentinel", "--since", "2026-10-17T09:17:00Z"],
        ["request", "new", "--from", "owner", "--severity", "normal", "--where", "the sign-in page", "--body", FILE],
        ["triage", "apply", "--body", FILE],
        ["queue", "next"],
        ["queue", "bundle"],
        ["queue", "bundle", "--through", "abc1234"],
        ["queue", "check", "abc1234", "--body", FILE],
        ["queue", "task", "new", "--body", FILE, "--requests", "r-2026-10-09-0001", "--title", "T", "--goal-item", "G1",
         "--severity", "major", "--surfaces", "ui,api", "--cause", "x.py:1", "--slices", "1", "--owner-class", "none"],
        ["queue", "task", "facts", "t-0001", "--surfaces", "ui", "api", "--slices", "2"],
        ["queue", "task", "status", "t-0001", "turn"],
        ["route", "t-0001"],
        ["propose", "t-0001", "--picture", FILE, "--why", "now"],
        ["propose", "--none"],
        ["spec", "save", "t-0001", FILE, "--slices", "3"],
        ["brief", "comments", "t-0001"],
        ["brief", "reply", "t-0001", "c1", "Cut to 180 ms."],
        ["needs-owner", "t-0001", "--picture", FILE, "--question", "Why?"],
        ["turn", "next"],
        ["slot", "claim"],
        ["slot", "up", "web"],
        ["slot", "up", "--at", "abc1234"],
        ["slot", "restart", "web", "worker"],
        ["slot", "stop", "relay", "control"],
        ["slot", "free", "2"],
        ["slot", "sign-in", "tester-1@example.com"],
        ["slot", "seed", "load", "basic", "--as", "tester-1@example.com"],
        ["slot", "desktop", "up", "tester-1@example.com"],
        ["slot", "run", "web", "--", "du", "-sh", "/app"],
        ["qa", "plan"],
        ["qa", "result", "--head", "abc1234"],
        ["qa", "step", "--head", "abc1234", "--step", "sign-in:2"],
        ["qa", "step-result", "--head", "abc1234", "--step", "sign-in:2", "--result", "fail", "--got", "no form"],
        ["qa", "finding", "--head", "abc1234", "--severity", "major", "--where", "/cart", "--body", FILE],
        ["qa", "waive", "t-0001", "f-2", "--why", "the spec asks for it"],
        ["qa-ready", "abc1234", "--slices", "abc1234", "def5678"],
        ["qa-ready", "abc1234", "--reproduce"],
        ["qa-clean"],
        ["ready-check"],
    ],
)  # fmt: skip
def test_every_command_asks_for_a_verb_the_warden_and_the_guard_know(argv: list[str]) -> None:
    call = _build(argv)
    assert call.verb in VERBS
    assert verb_of(["factory", *argv]) == call.verb


def test_a_slot_run_hands_on_every_word_after_the_double_dash() -> None:
    call = _build(["slot", "run", "web", "--", "python", "-c", "print(1)", "--verbose"])
    assert call.args == {"op": "run", "service": "web", "command": ["python", "-c", "print(1)", "--verbose"]}


def test_a_reproduction_asks_for_one_and_a_ready_head_does_not() -> None:
    assert _build(["qa-ready", "abc1234", "--reproduce"]).args == {"head": "abc1234", "reproduce": True}
    assert _build(["qa-ready", "abc1234"]).args == {"head": "abc1234"}


def test_task_facts_arrive_typed() -> None:
    call = _build(["queue", "task", "facts", "t-0001", "--surfaces", "ui,", "api", "--slices", "2", "--after", "t-2"])
    assert call.args == {"task": "t-0001", "surfaces": ["ui", "api"], "slices": 2, "after": ["t-2"]}


def test_a_step_mark_names_its_step_as_a_finding_does() -> None:
    call = _build(
        ["qa", "step-result", "--head", "abc1234", "--step", "cart:2", "--result", "fail", "--got", "no total"]
    )
    assert call.args == {"head": "abc1234", "item": "cart", "step": 2, "result": "fail", "got": "no total"}
    # Words after an option are lost to Python 3.12's argparse, which the factory runs on: the step is an option.
    with pytest.raises(CliError, match="--step <item>:<n>"):
        _build(["qa", "step", "cart", "2", "--head", "abc1234"])


def test_telemetry_is_read_from_prod_or_a_slot_and_staging_is_slot_0() -> None:
    assert _build(["errors", "--where", "slot", "0"]).args == {"where": "slot", "slot": 0}
    assert _build(["logs", "--find", "sentinel"]).args == {"where": "prod", "find": "sentinel"}
    # Agents leave a text of several words unquoted; it is still one text.
    found = _build(["logs", "--find", "connection", "refused", "--since", "2026-10-17T17:40:00Z"]).args
    assert found == {"where": "prod", "find": "connection refused", "since": "2026-10-17T17:40:00Z"}
    with pytest.raises(CliError, match="staging is slot 0"):
        _build(["errors", "--where", "staging"])


def test_propose_none_names_no_task() -> None:
    with pytest.raises(CliError):
        _build(["propose", "t-0001", "--none"])


def test_the_subagent_id_the_guard_puts_first_travels_with_the_request() -> None:
    call = _build(["--agent-id", "a1b2c3", "qa", "result", "--head", "abc1234"])
    assert (call.verb, call.agent_id) == ("qa.result", "a1b2c3")
    assert _build(["qa-clean"]).agent_id == ""
    with pytest.raises(CliError):
        _build(["--agent-id", "--head"])


def test_a_testers_ready_check_makes_the_folder_it_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "worktree" / ".factory" / "a1b2c3"
    answer = {"ok": True, "head": "a" * 40, "slot": 1, "testers": [], "folder": str(folder)}
    monkeypatch.setattr(factory, "ask", lambda path, call: answer)
    assert factory.main(["ready-check"], Places.under(tmp_path)) == 0
    assert folder.is_dir()


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


@pytest.mark.parametrize(
    "line",
    [
        "cause --tried 'the cache, and a cold start loops too'", "queue task show t-0001", "queue next",
        "queue task status t-0001 approved", "queue task facts t-0001 --slices 2", "turn next", "check",
        "qa finding --head abc --severity major --where /notes --step notes:2 --body f.md", "qa result --head abc",
        "qa plan", "qa scenario sign-ins-cancel",
        "qa step --head abc --step notes:2", "qa step-result --head abc --step notes:2 --result pass --got 'shown'",
        "qa waive t-0001 f-1 --why 'not a bug'", "qa-ready abc", "qa-clean", "ready-check", "triage plan --body p.json",
        "triage apply", "inbox", "inbox --wait", "tick", "status", "reflect --kind miss 'it missed'",
        "slot claim", "slot up", "propose t-0001 --picture p.svg --why now", "spec save t-0001 s.md --slices 2",
        "needs-owner t-0001 --picture p.svg --question why", "errors", "logs", "doing 'reading'",
    ],
)  # fmt: skip
def test_every_command_asks_for_the_verb_the_guard_reads_it_as(line: str) -> None:
    argv = shlex.split(line)
    call = build(argv, lambda path: b"text")
    assert verb_of(["factory", *argv]) == call.verb and call.verb in VERBS


def test_a_body_given_as_text_instead_of_a_file_says_to_write_the_file_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "The save button does nothing and says nothing, " * 6
    argv = ["qa", "finding", "--head", "abc1234", "--severity", "minor", "--where", "/notes", "--body", text]
    assert factory.main(argv, Places.under(tmp_path)) == 2
    assert "write the text to a file first, then name the file" in capsys.readouterr().err
