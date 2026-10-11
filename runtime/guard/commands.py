"""The guard's rules for a shell command: every program it would run, found through wrappers, nested shells, heredocs
and substitutions, meets each rule in turn, then the command as a whole meets the rules that need all of it.

A factory agent may not start or resume a Claude session itself (`raw-starts`), step outside the factory's slice
(`leave-slice`), reach prod, move another session's branch, skip a git hook, write a protected path, run a
factory verb that is not its role's, or run a command the guard cannot read.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from runtime.guard import branches, expansions, factory_calls, git_rules, nested, prod, scratch, shell, writes
from runtime.guard.facts import Call, Decision, Refusal, inside, resolve
from runtime.guard.wire import PARENTS, READ_TOOLS

# `claude` subcommands that only read.
_CLAUDE_READS = frozenset({"agents", "logs", "--version", "-v", "--help", "-h"})
# Programs that would run work outside the factory's slice and its limits, or reach a machine or a container.
_OUTSIDE = frozenset(
    {"sudo", "systemd-run", "run0", "at", "batch", "crontab", "pkexec", "su", "doas", "machinectl", "dbus-send",
     "gdbus", "docker", "docker-compose", "podman", "podman-compose", "nerdctl", "ssh", "scp", "sftp", "xdg-open",
     "gio"}
)  # fmt: skip
# Read-only programs: a call made only of these is a way out, never refused for an unpermitted session.
_READERS = frozenset({"ls", "cat", "head", "tail", "wc", "grep", "rg", "pwd", "echo", "true", "date", "stat", "find",
                      "diff", "file"})  # fmt: skip
# A reader's flags that write files or run programs.
_NOT_READING = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls", "--pre"}
)
SHELL_TOOLS = frozenset({"Bash", "Monitor"})

Rule = Callable[[Call, shell.Invocation, Path], Decision]


def is_way_out(call: Call) -> bool:
    """Reading, reporting and talking to its own parent: an unpermitted or paused session may still do these."""
    if call.tool in READ_TOOLS:
        return True
    if call.tool == "SendMessage":
        return str(call.tool_input.get("to", "")) in PARENTS
    return call.tool in SHELL_TOOLS and reads_only(str(call.tool_input.get("command", "")))


def reads_only(command: str) -> bool:
    try:
        found = shell.invocations(command)
    # An unreadable command is no way out; the shell rules refuse it with the reason.
    except shell.ShellError:
        return False
    return bool(found) and all(_reader(inv) for inv in found)


def _reader(inv: shell.Invocation) -> bool:
    if inv.writes:
        return False
    if inv.program in _READERS:
        return not any(a.split("=", 1)[0] in _NOT_READING for a in inv.argv[1:])
    args = git_rules.git_args(inv)
    return args is not None and git_rules.subcommand(args)[0] in git_rules.READS


def check_bash(call: Call) -> Decision:
    """Every program a shell command would run, then the command as a whole."""
    command = str(call.tool_input.get("command", ""))
    try:
        found = shell.invocations(command)
    # The refusal names the parse error to the agent.
    except shell.ShellError as exc:
        if call.off("unreadable-command"):
            return None
        return Refusal("unreadable-command", f"the guard cannot read this command ({exc}); write it plainly")
    forged = factory_calls.check_forged(call, found)
    if forged is not None:
        return forged
    cwd = call.cwd
    for inv in found:
        cwd = _after_cd(call, inv, cwd)
        for rule in _RULES:
            decision = rule(call, inv, cwd)
            if decision is not None:
                return decision
    return _whole(call, command, found, cwd)


def _whole(call: Call, command: str, found: list[shell.Invocation], cwd: Path) -> Decision:
    readers = [inv for inv in found if nested.reads_its_script_from_stdin(inv.program, inv.argv[1:])]
    # Each shell that reads its standard input needs a heredoc body the guard read as its script.
    if len(readers) > shell.shell_heredocs(command) and not call.off("unreadable-command"):
        return Refusal(
            "unreadable-command",
            f"{readers[0].program} reads its commands from a pipe or a file the guard cannot read; run them plainly",
        )
    if not call.off("git-bypass") and any(map(git_rules.sets_config_from_env, found)):
        return Refusal("git-bypass", "git's configuration is the owner's, from the environment too")
    return factory_calls.check_alone(call, command, found, cwd)


def _after_cd(call: Call, inv: shell.Invocation, cwd: Path) -> Path:
    """Where the rest of the command runs once a `cd` or `pushd` ran; `cd` alone goes home."""
    if inv.program not in {"cd", "pushd"}:
        return cwd
    words = [a for a in inv.argv[1:] if not a.startswith("-")]
    return resolve(words[0], cwd, call.home) if words else call.home


def _raw_start(call: Call, inv: shell.Invocation, cwd: Path) -> Decision:
    args = inv.argv[1:]
    if inv.program != "claude" or call.off("raw-starts") or (args and args[0] in _CLAUDE_READS):
        return None
    return Refusal("raw-starts", "agents never start Claude sessions themselves; the orchestrator asks the warden")


def _leave_slice(call: Call, inv: shell.Invocation, cwd: Path) -> Decision:
    if call.off("leave-slice"):
        return None
    if inv.program in _OUTSIDE:
        return Refusal("leave-slice", f"{inv.program} would run work outside the factory's limits; ask the warden")
    if inv.program in {"systemctl", "busctl"} and "--user" in inv.argv[1:]:
        return Refusal("leave-slice", f"{inv.program} --user would act outside the factory's limits")
    return None


def _hand_pass(call: Call, inv: shell.Invocation, cwd: Path) -> Decision:
    """The checks run only through `factory check`, which the warden walls in and alone records a pass for."""
    if call.off("git-bypass"):
        return None
    runner = call.places.install / "checks"
    script = prod.script_of(inv)
    by_path = bool(script) and inside(resolve(script, cwd, call.home), runner)
    by_module = inv.program.startswith("python") and any(a.startswith("checks.") for a in inv.argv[1:])
    if by_path or by_module:
        return Refusal("git-bypass", "the checks run, and a pass is recorded, only through `factory check`")
    return None


def _git(call: Call, inv: shell.Invocation, cwd: Path) -> Decision:
    return git_rules.check(call, inv) or git_rules.github_writes(call, inv)


def _verb(call: Call, inv: shell.Invocation, cwd: Path) -> Decision:
    return factory_calls.check_verb(call, inv)


def _expanded(call: Call, inv: shell.Invocation, cwd: Path) -> Decision:
    return expansions.check(call, inv)


_RULES: tuple[Rule, ...] = (
    writes.check_shell,
    _raw_start,
    _leave_slice,
    prod.check,
    branches.check,
    _git,
    scratch.check_shell,
    _verb,
    _hand_pass,
    _expanded,
)
