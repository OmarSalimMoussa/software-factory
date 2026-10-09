from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from system_fakes import settings

from runtime.system import layout
from runtime.system.runners import launch_file as lf
from runtime.system.runners.stack_run import size_bytes

STATE = Path("/var/lib/software-factory")
HOME = Path("/home/software-agent")
REPO = Path("/home/software-agent/shop")
SESSION = {
    "kind": "session", "session_id": "0b8f6a52-3c1e-4f7a-9d2b-5e6c7d8e9f01", "name": "impl-cart",
    "model": "claude-opus-5-5", "effort": "high", "agent": "implementer",
    "settings": f"{STATE}/public/session-settings/r1.json", "agents": f"{STATE}/public/plugin/agents.json",
    "worktree": "impl-cart", "prompt": "-- build it",
}  # fmt: skip
SESSIONS = {"s1": {"worktree": f"{REPO}/.claude/worktrees/impl-cart"}, "s2": {"worktree": "/home/owner/elsewhere"}}


def _parse(raw: dict[str, Any]) -> object:
    kind = raw["kind"]
    if kind == lf.SESSION:
        return lf.session_argv(raw, STATE)
    if kind == lf.COMMAND:
        return lf.command_argv(raw, HOME)
    slot, _, session = lf.stack_parts(raw, settings())
    return lf.checkout_for(session, settings(), SESSIONS)


def test_each_kind_runs_only_the_installed_programs() -> None:
    argv = lf.session_argv(SESSION, STATE)
    command, cwd = lf.command_argv({"kind": "command", "argv": ["git", "worktree", "list"], "cwd": str(REPO)}, HOME)

    assert argv[0] == str(layout.CLAUDE_BIN)
    assert argv[-2:] == ["--", "-- build it"]  # a prompt that looks like a flag stays the prompt
    assert command[0] == "/usr/bin/git" and cwd == REPO
    assert lf.checkout_for("s1", settings(), SESSIONS) == REPO / ".claude" / "worktrees" / "impl-cart"
    assert lf.checkout_for("", settings(), SESSIONS) == REPO


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "command", "argv": ["sh", "-c", "id"], "cwd": str(HOME)},
        {"kind": "command", "argv": ["/usr/bin/git", "status"], "cwd": str(HOME)},
        {"kind": "command", "argv": ["git", "status"], "cwd": "/etc"},
        {"kind": "command", "argv": ["git", "status"], "cwd": f"{HOME}/../software-warden"},
        {"kind": "command", "argv": ["git", "status"], "cwd": "shop"},
        {**SESSION, "name": "--dangerously-skip-permissions"},
        {**SESSION, "model": "-x"},
        {**SESSION, "settings": f"{STATE}/public/session-settings/../launch/r1.json"},
        {**SESSION, "settings": "/tmp/r1.json"},
        {**SESSION, "agents": "/home/software-agent/agents.json"},
        {**SESSION, "session_id": "--resume"},
        {"kind": "stack", "slot": 9, "verb": ["up"], "session": "s1"},
        {"kind": "stack", "slot": True, "verb": ["up"], "session": "s1"},
        {"kind": "stack", "slot": 1, "verb": ["rm", "-rf"], "session": "s1"},
        {"kind": "stack", "slot": 1, "verb": [], "session": "s1"},
        {"kind": "stack", "slot": 1, "verb": ["up"], "session": "s2"},
    ],
)
def test_a_request_outside_its_bounds_is_refused_before_anything_runs(raw: dict[str, Any]) -> None:
    with pytest.raises(lf.BadRequest):
        _parse(raw)


@pytest.mark.parametrize(("text", "size"), [("0B", 0), ("512kB", 512_000), ("1.25GB", 1_250_000_000), ("2TB", 2e12)])
def test_docker_sizes_are_decimal(text: str, size: float) -> None:
    assert size_bytes(text) == size
