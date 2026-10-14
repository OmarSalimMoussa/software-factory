"""The factory command in a shell command: who may run which verb (`factory-verbs`, which also keeps the read-only
telemetry verbs to intake, triage and the testers among subagents), a task's status set only by the orchestrator
(`queue-status`), and the command alone in its line (`factory-alone`). The warden counts an implementer's check runs.

The factory command runs outside the sandbox so it can reach the warden. Claude Code exempts a call only when its
whole text is `factory` or `factory …`, so the guard asks for exactly that: one plain command, the bare word first,
nothing the shell would expand or join, and no file outside the session's worktree.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from runtime.guard import shell
from runtime.guard.facts import ORCHESTRATOR, Call, Decision, Refusal, Rewrite, inside, resolve
from runtime.slots import leases
from runtime.warden.verbs import VERBS

FACTORY = "factory"
# The flag that names a subagent to the warden; only the guard writes it, on a subagent's own calls.
AGENT_ID_FLAG = "--agent-id"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PLAIN_START = re.compile(r"^factory(\s|$)")
_SHELL_SPECIAL = frozenset("$`*?[{}")
_JOINS = {"|": "pipes the answer into another command", "&&": "adds a second command", "||": "adds a second command",
          ";": "adds a second command", "&": "runs it in the background", ">": "sends the answer to a file",
          ">>": "sends the answer to a file", "<": "feeds it a file", "<<": "feeds it a heredoc",
          "(": "runs it in a subshell", ")": "runs it in a subshell"}  # fmt: skip
_HELP = frozenset({"-h", "--help", "help"})
_FLAG_VERBS = {("inbox", "--wait"): "inbox.wait", ("propose", "--none"): "propose.none"}
_STARTS = frozenset({"implementer", "designer"})


def verb_of(argv: list[str]) -> str:
    """The verb a factory command line asks for, by the warden's names; empty when it asks for nothing."""
    args: list[str] = []
    rest = iter(argv[1:])
    for word in rest:
        if word == AGENT_ID_FLAG:
            next(rest, None)
        else:
            args.append(word)
    words = [w for w in args if not w.startswith("-")]
    flags = {w.split("=", 1)[0] for w in args if w.startswith("-")}
    if not words:
        return ""
    for (word, flag), verb in _FLAG_VERBS.items():
        if words[0] == word and flag in flags:
            return verb
    # `factory task status …`; the queue's verbs also read as `factory queue task new …` and `factory queue next`.
    candidates = [".".join(words[:2])]
    if words[0] == "queue" and len(words) > 1:
        candidates += [".".join(words[1:3]), words[1]]
    return next((c for c in candidates if c in VERBS), words[0])


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
    return _not_theirs(call, verb, inv)


def _not_theirs(call: Call, verb: str, inv: shell.Invocation) -> Decision:
    """The refusal when ``factory <verb>`` is not this caller's to run."""
    entry = VERBS.get(verb)
    actor = f"a subagent of the {call.role}" if call.in_subagent else f"the {call.role}"
    if entry is None or entry.owner_socket or call.role not in entry.roles:
        return Refusal("factory-verbs", f"`factory {verb}` is not for {actor}; see `factory --help`")
    if call.in_subagent and call.agent_type not in entry.kinds:
        return Refusal("factory-verbs", f"`factory {verb}` is not a {call.agent_type} subagent's to run; report to "
                                        "your parent instead")  # fmt: skip
    words = [w for w in inv.argv[2:] if not w.startswith("-")]
    if verb == "start" and words[:1] and words[0] not in _STARTS:
        return Refusal("factory-verbs", "the orchestrator starts implementers and designers; the owner starts the rest")
    return None


def _expanded(text: str) -> str:
    """The first character the shell would expand in ``text`` (``$`` and backticks even in double quotes), or ""."""
    quote, escaped = "", False
    for ch in text:
        if escaped:
            escaped = False
        elif quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            escaped = True
        elif ch in _SHELL_SPECIAL and (not quote or ch in "$`"):
            return ch
        elif ch in "'\"":
            quote = "" if quote == ch else quote or ch
    return ""


def _parse(command: str) -> tuple[list[str], str]:
    """The words of a command and, when it is not one plain `factory` call, why not: the words are then partial."""
    text = command.strip()
    if not _PLAIN_START.match(text):
        return [], "something comes before the bare word factory"
    if "\n" in text or "\r" in text:
        return [], "a line break starts a second command"
    expanded = _expanded(text)
    if expanded:
        return [], f"the shell would expand `{expanded}` there; only single quotes keep it as text"
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        words = list(lexer)
    # telemetry: surfaced — words shlex cannot split come back as no words, `[]`, and the reason
    except ValueError:
        return [], "a quote is never closed"
    joined = next((w for w in words if set(w) <= set("();<>|&")), None)
    if joined is not None:
        return words, f"`{joined}` {_JOINS.get(joined, 'joins a second command or a file')}"
    return words, ""


def plain_words(command: str) -> list[str] | None:
    """The words of a command that is one plain `factory` call, or None when the shell would read more into it."""
    words, why = _parse(command)
    return None if why else words


def check_alone(call: Call, command: str, found: list[shell.Invocation], cwd: Path) -> Decision:
    """A call that runs `factory` is that one plain call, and it reads files only from the session's own worktree."""
    if not any(inv.program == FACTORY for inv in found) or call.off("factory-alone"):
        return None
    words, why = _parse(command)
    if why:
        return Refusal(
            "factory-alone", f"{why}: run factory on its own, one plain call, and read its answer as printed"
        )
    worktree = Path(os.path.realpath(call.worktree))
    # Its arguments are read outside the sandbox, so a file it names must be the session's own; a slot run's command,
    # after its --, runs in the slot's container and reads nothing here.
    ran = words[1:3] == ["slot", leases.RUN] and "--" in words
    for word in words[1 : words.index("--") if ran else None]:
        for candidate in [word, word.split("=", 1)[1] if "=" in word else ""]:
            if not candidate or "://" in candidate or any(c.isspace() for c in candidate):
                continue
            path = resolve(candidate, cwd, call.home)
            if os.path.lexists(path) and not inside(Path(os.path.realpath(path)), worktree):
                return Refusal("factory-alone", f"the factory command reads files only from your worktree, {worktree}")
    return None


def check_forged(call: Call, found: list[shell.Invocation]) -> Decision:
    """No agent writes the subagent flag: only the guard does, so the warden can trust it."""
    if call.off("agent-id"):
        return None
    for inv in found:
        if inv.program == FACTORY and any(a == AGENT_ID_FLAG or a.startswith(AGENT_ID_FLAG + "=") for a in inv.argv):
            return Refusal("agent-id", f"{AGENT_ID_FLAG} is set only by the guard; run factory without it")
    return None


def stamp(call: Call) -> Decision:
    """A subagent's factory call names the subagent right after the program word, so the warden knows which
    subagent asks, not only its parent."""
    if not call.in_subagent or call.tool not in {"Bash", "Monitor"}:
        return None
    command = str(call.tool_input.get("command", ""))
    if plain_words(command) is None:
        return None
    if not _SAFE_ID.match(call.agent_id):
        return Refusal("agent-id", "this subagent's id cannot be passed to the factory safely; report to your parent")
    text = command.strip()
    stamped = f"{FACTORY} {AGENT_ID_FLAG} {call.agent_id}{text[len(FACTORY) :]}"
    return Rewrite({**call.tool_input, "command": stamped})
