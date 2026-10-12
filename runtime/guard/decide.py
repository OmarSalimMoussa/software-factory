"""The guard's decision for one hook input: who is calling, then each rule in turn.

A session the warden registered as the owner's is left alone. A retired session gets nothing, and a subagent the
owner stopped only its hand-back. A session the warden never started may only read and report; one being benched may
only commit and ask the factory. Every other call meets its role's rules; the first refusal wins, and a rewrite holds
only once every rule passed the call.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from runtime.guard import browser, commands, factory_calls, git_rules, scratch, shell, subagents, writes
from runtime.guard.facts import (
    ORCHESTRATOR,
    OWNER,
    UNPERMITTED,
    Call,
    Decision,
    Refusal,
    Rewrite,
    read_sessions,
    read_settings,
    read_switches,
)
from runtime.guard.wire import HANDBACK
from runtime.shared import store
from runtime.shared.models import BENCH_PENDING, RETIRED, WAITING, WORKING
from runtime.shared.paths import Places

UNPERMITTED_CARD = (
    "This session holds no permit from the factory's warden, so it may read and report, nothing more. "
    "Sessions start only through `factory start`."
)
RETIRED_NOTE = "This session is retired and can never run again: every call is refused. Stop now."
BENCH_NOTE = (
    "you are paused for higher-ranked work: commit your work in progress (`git add`, then `git commit`) and stop; "
    "you come back with your full context"
)
_BANNED_TOOLS = frozenset({"Workflow", "RemoteTrigger"})
# Claude Code's own worktrees hold branches own-branch never sees, and a subagent's starts from the default branch.
_WORKTREE_TOOLS = frozenset({"EnterWorktree", "ExitWorktree"})
_SCHEDULE_TOOLS = frozenset({"CronCreate", "ScheduleWakeup"})
_SCHEDULE_SKILLS = frozenset({"loop", "schedule"})
_INBOX_WAIT = re.compile(r"(?:^|\s)factory inbox --wait(?!\S)")


def resolve(payload: dict[str, Any], places: Places, now: datetime, home: Path) -> Call | None:
    """The call with the warden's published facts, or None for a session the warden registered as the owner's."""
    try:
        switches = read_switches(places)
    # telemetry: expected — every rule stays on; the warden reports the unreadable switches on the Guard page
    except store.StoreError:
        switches = frozenset()
    call = Call(payload, places, read_settings(places), switches, read_sessions(places), now, home)
    return None if call.role == OWNER else call


def _retired(call: Call) -> Decision:
    if call.session is None or call.session.state != RETIRED or call.off("retired"):
        return None
    return Refusal("retired", "this session is retired and can never run again; stop now")


def _stopped_subagent(call: Call) -> Decision:
    if not call.in_subagent or call.tool == HANDBACK or call.off("stopped-subagent"):
        return None
    if any(s.agent_id == call.agent_id and s.state == RETIRED for s in call.sessions.values()):
        return Refusal("stopped-subagent", "the owner stopped this subagent; hand back now with what you have")
    return None


def _unpermitted(call: Call) -> Decision:
    if call.role != UNPERMITTED or commands.is_way_out(call) or call.off("unpermitted"):
        return None
    return Refusal(
        "unpermitted", "this session holds no permit from the warden, so it may only read and report; stop here"
    )


def _bench_pending(call: Call) -> Decision:
    if call.session is None or call.session.state != BENCH_PENDING or call.off("bench-pending"):
        return None
    if commands.is_way_out(call) or (call.tool in commands.SHELL_TOOLS and _commits_only(call)):
        return None
    return Refusal("bench-pending", BENCH_NOTE)


def _commits_only(call: Call) -> bool:
    """A command made only of staging, committing and the factory command."""
    try:
        found = shell.invocations(str(call.tool_input.get("command", "")))
    # telemetry: expected — an unreadable command is not a commit; the refusal says what to run
    except shell.ShellError:
        return False
    for inv in found:
        args = git_rules.git_args(inv)
        sub = git_rules.subcommand(args)[0] if args is not None else ""
        if inv.program != factory_calls.FACTORY and sub not in {"add", "commit", "status"}:
            return False
    return bool(found)


def _tool(call: Call) -> Decision:
    """Banned tools, schedules, Claude Code's own worktree tools, and the path of an edit."""
    if call.tool in _BANNED_TOOLS and not call.off("banned-tools"):
        return Refusal("banned-tools", f"{call.tool} starts work the factory cannot count or stop; ask your parent")
    isolated = call.tool == "Agent" and bool(call.tool_input.get("isolation"))
    if (call.tool in _WORKTREE_TOOLS or isolated) and not call.off("worktree-tools"):
        return Refusal("worktree-tools", "the warden makes every worktree; stay in yours, and start subagents in it")
    skill = str(call.tool_input.get("skill", "")) if call.tool == "Skill" else ""
    if (call.tool in _SCHEDULE_TOOLS or skill in _SCHEDULE_SKILLS) and not call.off("schedules"):
        return Refusal("schedules", "the warden drives the loop and wakes you; keep no schedule of your own")
    if call.tool in writes.EDIT_TOOLS:
        return writes.check_edit(call) or scratch.check_edit(call)
    return None


_BY_TOOL: dict[str, Callable[[Call], Decision]] = {
    "Bash": commands.check_bash,
    "Monitor": commands.check_bash,
    "Agent": subagents.check_agent,
    "SendMessage": subagents.check_send,
}


def _by_tool(call: Call) -> Decision:
    rule = _BY_TOOL.get(call.tool)
    return rule(call) if rule else None


def decide_tool(call: Call) -> Decision:
    """Every PreToolUse rule: the first refusal wins, and each rewrite feeds the rules after it."""
    for gate in (_retired, _stopped_subagent, _unpermitted, _bench_pending):
        refusal = gate(call)
        if refusal is not None:
            return refusal
    current = call
    for rule in (subagents.check_tester_time, browser.check, _tool, _by_tool, subagents.check_intake_time):
        decision = rule(current)
        if isinstance(decision, Refusal):
            return decision
        if isinstance(decision, Rewrite):
            current = current.with_input(decision.updated_input)
    # Last, once every rule has read the command as the agent wrote it.
    stamped = factory_calls.stamp(current)
    if isinstance(stamped, Refusal):
        return stamped
    if isinstance(stamped, Rewrite):
        current = current.with_input(stamped.updated_input)
    return Rewrite(current.tool_input) if current.tool_input != call.tool_input else None


def session_context(call: Call) -> str:
    """What a session reads at start, on resume and after a compaction: where its goal is, and when to write to
    SELF_REFLECT.md; its role is its system prompt. It only informs; the PreToolUse rules refuse."""
    if call.session is not None and call.session.state == RETIRED:
        return RETIRED_NOTE
    if call.session is None:
        return UNPERMITTED_CARD
    goal = call.worktree / "GOAL.md"
    parts = [
        f"The product's goal is {goal}: read it before you act. It says who the product is for, the ranked goal, "
        "how it looks and how it speaks. Only a person edits it.",
        _read(call.places.plugin / "self-reflect.md", "the note on when to reflect"),
    ]
    if call.session.state == BENCH_PENDING:
        parts.append("Now " + BENCH_NOTE + ".")
    return "\n\n".join(parts)


def _read(path: Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    # telemetry: surfaced — the session reads in its context that the file `what` names is unreadable
    except OSError as exc:
        return f"{what} ({path}) cannot be read ({type(exc).__name__}): tell the owner the factory's install is broken."


def check_stop(call: Call) -> Decision:
    """The orchestrator may not go idle without its wait for the warden's events in the background. A stop the hook
    already turned back once is let through, so the refusal names the whole remedy."""
    if call.role != ORCHESTRATOR or call.in_subagent or call.payload.get("stop_hook_active"):
        return None
    if call.session is None or call.session.state not in (WORKING, WAITING) or call.off("inbox-wait"):
        return None
    tasks = call.payload.get("background_tasks")
    waits = isinstance(tasks, list) and any(
        isinstance(t, dict) and t.get("type") == "shell" and _INBOX_WAIT.search(str(t.get("command", "")))
        for t in tasks
    )
    if waits:
        return None
    return Refusal(
        "inbox-wait",
        "your wait for the warden's events is gone: run `factory inbox --wait` as a background command "
        "(Bash with run_in_background), then stop",
    )
