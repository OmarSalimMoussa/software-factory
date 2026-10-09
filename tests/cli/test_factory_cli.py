"""The `factory` command: it reads the caller's files itself and sends bytes, never a path, and every command line
asks for the verb the guard reads from it."""

from __future__ import annotations

import io
import json
import os

import pytest

from runtime.cli import factory
from runtime.cli.factory import Call, CliError, build
from runtime.guard.factory_calls import verb_of
from runtime.warden.verbs import VERBS

FILE = "/home/agent/wt/picture.svg"
CONTENT = b"<svg/>"
BODY = b"the body"


def _build(argv: list[str]) -> Call:
    def read_file(path: str) -> bytes:
        assert path == FILE
        return CONTENT

    return build(argv, read_file, lambda: BODY)


@pytest.mark.parametrize(
    "argv",
    [
        ["propose", "t-0001", "--picture", FILE, "--why", "first visit", "--run", "2", "--reason", "shaky cause"],
        ["needs-owner", "t-0001", "--picture", FILE, "--brief", "https://claude.ai/code/artifact/x", "--ask", "Go?"],
        ["needs-owner", "t-0001", "--picture", FILE, "--question", "Keep the old link?"],
        ["spec", "save", "t-0001", FILE],
    ],
)
def test_a_named_file_is_sent_as_its_bytes_never_as_its_path(argv: list[str]) -> None:
    call = _build(argv)
    assert call.data == CONTENT
    assert FILE not in json.dumps(call.args) and "picture" not in call.args


@pytest.mark.parametrize(
    "argv",
    [
        ["start", "implementer", "t-0001"],
        ["stop", "impl-t-0001"],
        ["resume", "impl-t-0001", "go on"],
        ["extend", "impl-t-0001", "qa"],
        ["bench", "impl-t-0001", "--for", "t-0002"],
        ["revive", "impl-t-0001"],
        ["doing", "fixing the cookie"],
        ["tick"],
        ["inbox"],
        ["inbox", "--wait"],
        ["push"],
        ["promote", "--back"],
        ["reflect", "--kind", "friction", "a rule slowed me"],
        ["status"],
        ["settings", "show"],
        ["request", "new", "--from", "owner", "--severity", "normal", "--where", "the sign-in page"],
        ["triage", "apply"],
        ["queue", "next"],
        ["queue", "check", "abc1234"],
        ["queue", "task", "new", "--requests", "r-2026-10-09-0001", "--title", "T", "--goal-item", "G1",
         "--severity", "major", "--surfaces", "ui,api", "--cause", "x.py:1", "--slices", "1", "--owner-class", "none"],
        ["queue", "task", "facts", "t-0001", "--surfaces", "ui", "api", "--slices", "2"],
        ["queue", "task", "status", "t-0001", "turn"],
        ["route", "t-0001"],
        ["propose", "t-0001", "--picture", FILE, "--why", "now"],
        ["propose", "--none"],
        ["spec", "save", "t-0001", FILE],
        ["card", "comments", "t-0001", "3"],
        ["needs-owner", "t-0001", "--picture", FILE, "--question", "Why?"],
        ["merge", "recorded", "impl-t-0001", "abc1234"],
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


def test_a_body_is_only_what_already_waits_on_stdin() -> None:
    read, write = os.pipe()
    with os.fdopen(read, "rb") as empty, os.fdopen(write, "wb"), pytest.raises(CliError, match="nothing waits"):
        factory.read_stdin(empty)
    read, write = os.pipe()
    os.write(write, b"x")
    os.close(write)
    with os.fdopen(read, "rb") as heredoc:
        assert factory.read_stdin(heredoc) == b"x"


def test_a_refusal_is_printed_with_its_rule_and_fails() -> None:
    out, err = io.StringIO(), io.StringIO()
    assert factory.show({"ok": False, "refused": "max-proposals", "reason": "wait"}, out, err) == 1
    assert err.getvalue() == "factory: refused (max-proposals): wait\n"
