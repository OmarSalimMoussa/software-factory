"""The factory command in a shell command: who may run which verb (`factory-verbs`), a task's status set only by the
orchestrator (`queue-status`), the command alone in its line (`factory-alone`), and an implementer's check runs
(`check-runs`).

The factory command runs outside the sandbox so it can reach the warden. That exception must carry nothing else: no
other program, wrapper, redirect or variable in the same command, and no file outside the session's worktree.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from runtime.guard import shell, transcript
from runtime.guard.facts import IMPLEMENTER, ORCHESTRATOR, Call, Decision, Refusal, Rewrite, inside, resolve
from runtime.guard.wire import REFUSED
from runtime.warden.verbs import VERBS

FACTORY = "factory"
# The variable the factory command sends as a subagent's id; only the guard sets it, on a subagent's own calls.
AGENT_ID_VARIABLE = "SOFTWARE_AGENT_ID"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Verbs the command answers itself, without the warden.
LOCAL = frozenset({"check"})
# What a subagent never asks for: what steers its session's lifecycle, the loop, the queue's order, a merge or a
# check run. Its parent does.
SESSION_ONLY = frozenset(
    {"start", "stop", "resume", "extend", "bench", "revive", "doing", "tick", "inbox", "inbox.wait", "push", "propose",
     "propose.none", "route", "queue.next", "task.status", "merge.recorded", "needs-owner", "spec.save",
     "card.comments", "qa-ready", "qa-clean", "check"}
)  # fmt: skip
_HELP = frozenset({"-h", "--help", "help"})
_FLAG_VERBS = {("inbox", "--wait"): "inbox.wait", ("propose", "--none"): "propose.none"}
_STARTS = frozenset({"implementer", "designer"})


def verb_of(argv: list[str]) -> str:
    """The verb a factory command line asks for, by the warden's names; empty when it asks for nothing."""
    words = [w for w in argv[1:] if not w.startswith("-")]
    flags = {w.split("=", 1)[0] for w in argv[1:] if w.startswith("-")}
    if not words:
        return ""
    for (word, flag), verb in _FLAG_VERBS.items():
        if words[0] == word and flag in flags:
            return verb
    # `factory task status …`; the queue's verbs also read as `factory queue task new …` and `factory queue next`.
    candidates = [".".join(words[:2])]
    if words[0] == "queue" and len(words) > 1:
        candidates += [".".join(words[1:3]), words[1]]
    return next((c for c in candidates if c in VERBS or c in LOCAL), words[0])


def runs_check(inv: shell.Invocation) -> bool:
    """Whether an invocation runs the checks, not only lists them or asks for help."""
    return inv.program == FACTORY and verb_of(inv.argv) == "check" and not ({"--list", *_HELP} & set(inv.argv))


def check_verb(call: Call, inv: shell.Invocation) -> Decision:
    if inv.program != FACTORY or _HELP & set(inv.argv[1:]):
        return None
    verb = verb_of(inv.argv)
    if not verb or "$" in verb or "`" in verb:
        return None  # the unreadable-command rule refuses a verb known only once expanded
    if verb == "task.status" and (call.role != ORCHESTRATOR or call.in_subagent) and not call.off("queue-status"):
        return Refusal("queue-status", "only the orchestrator sets a task's status; tell it what changed")
    if call.off("factory-verbs"):
        return None
    entry = VERBS.get(verb)
    actor = f"a subagent of the {call.role}" if call.in_subagent else f"the {call.role}"
    if verb not in LOCAL and (entry is None or entry.owner_socket or call.role not in entry.roles):
        return Refusal("factory-verbs", f"`factory {verb}` is not for {actor}; see `factory --help`")
    if call.in_subagent and verb in SESSION_ONLY:
        return Refusal("factory-verbs", f"`factory {verb}` is your parent's to run; report to it instead")
    words = [w for w in inv.argv[2:] if not w.startswith("-")]
    if verb == "start" and words[:1] and words[0] not in _STARTS:
        return Refusal("factory-verbs", "the orchestrator starts implementers and designers; the owner starts the rest")
    return None


def check_alone(call: Call, found: list[shell.Invocation], cwd: Path) -> Decision:
    """`factory` runs outside the sandbox, so its command holds nothing else, and it reads files only from the
    session's own worktree."""
    runs = [inv for inv in found if inv.program == FACTORY]
    if not runs or call.off("factory-alone"):
        return None
    inv = runs[0]
    if len(found) > 1 or inv.wrappers or inv.writes or inv.assigned:
        return Refusal(
            "factory-alone", "run `factory` alone: no other program, wrapper, pipe, output or variable beside it"
        )
    worktree = Path(os.path.realpath(call.worktree))
    # Its arguments and its standard input are read outside the sandbox, so they come from the worktree.
    for word in [*inv.argv[1:], *inv.reads]:
        for candidate in [word, word.split("=", 1)[1] if "=" in word else ""]:
            if not candidate or "://" in candidate or any(c.isspace() for c in candidate):
                continue
            path = resolve(candidate, cwd, call.home)
            if os.path.lexists(path) and not inside(Path(os.path.realpath(path)), worktree):
                return Refusal("factory-alone", f"the factory command reads files only from your worktree, {worktree}")
    return None


def check_runs(call: Call, found: list[shell.Invocation]) -> Decision:
    """An implementer runs the checks at most `rounds.check_runs` times, plus what it was extended by; a run the
    guard refused never ran."""
    if call.role != IMPLEMENTER or call.in_subagent or call.off("check-runs") or not any(map(runs_check, found)):
        return None
    current = str(call.payload.get("tool_use_id", ""))
    refused = transcript.refused_ids(call.transcript, REFUSED)
    runs = 0
    for use in transcript.tool_uses(call.transcript, frozenset({"Bash"})):
        if use.tool_use_id == current or use.tool_use_id in refused:
            continue
        try:
            runs += any(map(runs_check, shell.invocations(str(use.args.get("command", "")))))
        # A command the guard could not read was refused, so it never ran.
        except shell.ShellError:
            continue
    limit = call.settings.get_int("rounds.check_runs") + call.extension("check_runs")
    if runs >= limit:
        return Refusal("check-runs", f"{runs} check runs already (limit {limit}); tell the orchestrator what fails")
    return None


def check_forged(call: Call, command: str) -> Decision:
    """No command names the subagent id variable: only the guard sets it, so the warden can trust it."""
    if AGENT_ID_VARIABLE not in command or call.off("agent-id"):
        return None
    return Refusal("agent-id", f"{AGENT_ID_VARIABLE} is set only by the guard; run your command without it")


def stamp(call: Call) -> Decision:
    """A subagent's factory call carries its id, so the warden knows which subagent asks, not only its parent."""
    if not call.in_subagent or call.tool not in {"Bash", "Monitor"}:
        return None
    command = str(call.tool_input.get("command", ""))
    try:
        found = shell.invocations(command)
    # The shell rules already refused a command the guard cannot read, or their switch is off.
    except shell.ShellError:
        return None
    if not any(inv.program == FACTORY for inv in found):
        return None
    if not _SAFE_ID.match(call.agent_id):
        return Refusal("agent-id", "this subagent's id cannot be passed to the factory safely; report to your parent")
    return Rewrite({**call.tool_input, "command": f"export {AGENT_ID_VARIABLE}={call.agent_id}; {command}"})
