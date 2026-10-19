"""Read a shell command the way the guard needs to: every program it would run, wrappers and nested shells unwrapped.

``sh -c '...'``, ``env``, ``nohup``, ``setsid``, ``timeout``, ``xargs``, ``tmux``, ``screen``, ``script``, ``eval``,
``systemd-run`` and command substitution all hide a program behind another; the guard must see the hidden one. A
program started from Python or a script file is out of reach, so the warden backs this up. Stdlib only.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from runtime.guard import heredocs, nested, wrappers

# What stands for a command's output in the words the guard reads: no rule may take it for a program or a flag.
EXPANDED = "$()"
_OPERATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")", "|&", ";;", "\n"})
_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>"})
_READS = frozenset({"<", "<<<", "<&", ">&"})
# Wrappers whose flags and option values come before the program they run.
# A shell keyword that opens a segment runs the command after it, so it is read like a wrapper.
_KEYWORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{", "coproc"})
_PLAIN_WRAPPERS = frozenset({"nohup", "time", "builtin", "caffeinate", "unbuffer", "busybox"}) | _KEYWORDS
# A descriptor written against its redirect (`2>&1`, `2>/dev/null`): the lexer would split it off as an argument.
_DESCRIPTOR = re.compile(r"(?:(?<=^)|(?<=[\s;&|(]))\d+(?=[<>])")
_FLAG_WRAPPERS = {
    "exec": frozenset({"-a"}),
    "setsid": frozenset(),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "--class", "--classdata"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "sudo": frozenset({"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "--user", "--group"}),
    "chrt": frozenset(),
    "taskset": frozenset(),
    "xargs": frozenset({"-I", "-L", "-n", "-P", "-d", "-E", "-s", "-a", "--max-args", "--max-procs"}),
    "flock": frozenset({"-w", "--timeout", "-E", "--conflict-exit-code"}),
}
_MAX_DEPTH = 6


@dataclass(slots=True)
class Invocation:
    """One program the command would run, with the wrappers it was found behind and the files it would write."""

    argv: list[str]
    wrappers: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    # The variables set for this program alone (`VAR=value cmd`, `env VAR=value cmd`).
    assigned: list[str] = field(default_factory=list)
    # The files redirected into its standard input (`< file`).
    reads: list[str] = field(default_factory=list)

    @property
    def program(self) -> str:
        return PurePosixPath(self.argv[0]).name if self.argv else ""


class ShellError(Exception):
    """The command cannot be read (an unbalanced quote or substitution); the guard treats it as unsafe."""


def invocations(command: str) -> list[Invocation]:
    """Every program ``command`` would run, nested ones included."""
    found: list[Invocation] = []
    try:
        plain, scripts, bodies, _ = heredocs.split(command)
    except heredocs.Unclosed as exc:
        raise ShellError(str(exc)) from exc
    _walk(plain, [], found, 0)
    for script in scripts:
        _walk(script, ["<<"], found, 1)
    for body in bodies:
        for substituted in _body_substitutions(body):
            _walk(substituted, ["<<", "$()"], found, 1)
    return found


def shell_heredocs(command: str) -> int:
    """How many heredoc bodies in ``command`` a shell reads from its standard input as its script."""
    return heredocs.split(command)[3]


def _body_substitutions(body: str) -> list[str]:
    """The `$( … )` and backtick commands of a heredoc body; its quotes are text, so only escapes matter."""
    inner: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
        elif body.startswith("$(", i):
            end, ran = _substitution(body, i)
            inner.extend(ran)
            i = end + 1
        elif body[i] == "`":
            end = body.find("`", i + 1)
            if end < 0:
                raise ShellError("an unclosed backtick")
            inner.append(body[i + 1 : end])
            i = end + 1
        else:
            i += 1
    return inner


def _walk(command: str, wrappers: list[str], found: list[Invocation], depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise ShellError("the command nests shells too deeply to read")
    plain, inner = _substitutions(command)
    for substituted in inner:
        _walk(substituted, [*wrappers, "$()"], found, depth + 1)
    for segment in _segments(plain):
        _unwrap(segment.argv, segment, wrappers, found, depth)


def _substitutions(command: str) -> tuple[str, list[str]]:
    """The command with ``$(...)`` and backtick substitutions replaced by `EXPANDED`, and their contents."""
    out: list[str] = []
    inner: list[str] = []
    i, quote = 0, ""
    while i < len(command):
        ch = command[i]
        if quote == "'":
            out.append(ch)
            quote = "" if ch == "'" else quote
            i += 1
            continue
        if ch == "\\" and i + 1 < len(command):
            out.append(command[i : i + 2])
            i += 2
            continue
        if ch == "'" and not quote:
            quote = "'"
        elif ch == '"':
            quote = "" if quote == '"' else '"'
        if command.startswith("$(", i):
            end, ran = _substitution(command, i)
            inner.extend(ran)
            out.append(EXPANDED)
            i = end + 1
            continue
        if ch == "`":
            end = command.find("`", i + 1)
            if end < 0:
                raise ShellError("an unclosed backtick")
            inner.append(command[i + 1 : end])
            out.append(EXPANDED)
            i = end + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), inner


def _substitution(command: str, i: int) -> tuple[int, list[str]]:
    """Where the ``$(`` at ``i`` ends, and what runs: of ``$(( … ))``, not ``$((a); (b))``, only its substitutions."""
    end = _closing(command, i + 2)
    body = command[i + 2 : end]
    if body.startswith("(") and _closing(body, 1) == len(body) - 1:
        return end, _substitutions(body[1:-1])[1]
    return end, [body]


def _closing(command: str, start: int) -> int:
    """The index of the ``)`` that closes a ``$(`` opened just before ``start``.

    A substitution starts its own quoting, so a bracket inside quotes in it is text, and a nested one is skipped whole.
    """
    depth, quote, i = 1, "", start
    while i < len(command):
        ch = command[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\":
            i += 1
        elif command.startswith("$(", i):
            i = _closing(command, i + 2)
        elif ch == '"':
            quote = "" if quote == '"' else '"'
        elif not quote and ch == "'":
            quote = "'"
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ShellError("an unclosed $(")


@dataclass(slots=True)
class _Segment:
    """One simple command's words, and the files its redirects write and read."""

    argv: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)


def _segments(command: str) -> list[_Segment]:
    lexer = shlex.shlex(_DESCRIPTOR.sub("", command.replace("\n", " ; ")), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise ShellError(f"the command cannot be read: {exc}") from exc
    segments = [_Segment()]
    pending = ""
    for token in tokens:
        if pending:
            if pending == ">":
                segments[-1].writes.append(token)
            elif pending == "<":
                segments[-1].reads.append(token)
            pending = ""
        elif token in _REDIRECTS:
            pending = ">"
        elif token == "<":
            pending = "<"
        elif token in _READS or token in ("<<", "<<-"):
            pending = "-"  # a here-string, a heredoc's delimiter or a descriptor: no file
        elif token in _OPERATORS or set(token) <= set("&|;()<>"):
            segments.append(_Segment())
        else:
            segments[-1].argv.append(token)
    return [s for s in segments if s.argv or s.writes or s.reads]


def _unwrap(
    argv: list[str],
    redirects: _Segment,
    wrappers: list[str],
    found: list[Invocation],
    depth: int,
    assigned: tuple[str, ...] = (),
) -> None:
    while argv and "=" in argv[0] and not argv[0].startswith(("-", "/", ".")):
        assigned = (*assigned, argv[0].split("=", 1)[0])  # VAR=value prefixes
        argv = argv[1:]
    if not argv:
        # A bare assignment can still reach every program after it (`set -a`), so it is kept with its names.
        if redirects.writes or redirects.reads or assigned:
            found.append(_invocation([], redirects, wrappers, assigned))
        return
    program = PurePosixPath(argv[0]).name
    strip = _STRIPS.get(program)
    if strip is not None:
        rest = strip(program, argv[1:], wrappers, found, depth)
        _unwrap(rest, redirects, [*wrappers, program], found, depth, assigned)
        return
    found.append(_invocation(argv, redirects, wrappers, assigned))
    for script in nested.scripts(program, argv[1:]):
        _walk(script, [*wrappers, program], found, depth + 1)


def _invocation(argv: list[str], redirects: _Segment, wrappers: list[str], assigned: tuple[str, ...]) -> Invocation:
    return Invocation(list(argv), list(wrappers), list(redirects.writes), list(assigned), list(redirects.reads))


Strip = Callable[[str, list[str], list[str], list[Invocation], int], list[str]]


def _strip_env(
    program: str,
    rest: list[str],
    wrappers: list[str],
    found: list[Invocation],
    depth: int,
) -> list[str]:
    """``env``'s own flags; ``env -S`` carries a whole command line of its own. Its assignments stay for the reader,
    which keeps them with the program they are set for."""
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"} and rest:
            value = rest.pop(0)
            if flag in {"-S", "--split-string"}:
                _walk(value, [*wrappers, "env"], found, depth + 1)
        elif flag.startswith("--split-string="):
            _walk(flag.partition("=")[2], [*wrappers, "env"], found, depth + 1)
        elif flag.startswith("-S") and len(flag) > 2:
            _walk(flag[2:], [*wrappers, "env"], found, depth + 1)
    return rest


def _strip_flags(
    program: str,
    rest: list[str],
    wrappers: list[str],
    found: list[Invocation],
    depth: int,
) -> list[str]:
    """A wrapper with flags of its own (``sudo``, ``nice``, ``xargs``): it is itself an invocation the rules see."""
    valued = _FLAG_WRAPPERS[program]
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in valued and rest:
            rest.pop(0)
    if program in {"chrt", "taskset"} and rest:
        rest.pop(0)  # the priority or the CPU mask
    found.append(Invocation(argv=[program], wrappers=list(wrappers), writes=[]))
    return rest


_STRIPS: dict[str, Strip] = {
    "env": _strip_env,
    "uv": wrappers.uv,
    "uvx": wrappers.uv,
    "timeout": wrappers.timeout,
    "function": wrappers.function,
    "command": wrappers.command,
    **{program: wrappers.plain for program in _PLAIN_WRAPPERS},
    **{program: _strip_flags for program in _FLAG_WRAPPERS},
}
