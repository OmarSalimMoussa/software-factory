"""Read a shell command the way the guard needs to: every program it would run, wrappers and nested shells unwrapped.

``sh -c '...'``, ``env``, ``nohup``, ``setsid``, ``timeout``, ``xargs``, ``tmux``, ``screen``, ``script``, ``eval``,
``systemd-run`` and command substitution all hide a program behind another; the guard must see the hidden one. A
program started from Python or a script file is out of reach, so the warden backs this up. Stdlib only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from runtime.guard import heredocs, nested, wrappers

_PUNCTUATION = frozenset("();<>|&")
# A word that sets a variable for the program after it; any other word holding `=` is a program or an argument.
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?\+?=")
_DOUBLE_QUOTE_ESCAPE = re.compile(r'\\(["\\$`])')
_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>"})
_READS = frozenset({"<", "<<<", "<&", ">&"})
# A shell keyword that opens a segment runs the command after it, so it is read like a wrapper.
_KEYWORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{", "coproc"})
_PLAIN_WRAPPERS = frozenset({"nohup", "time", "builtin", "caffeinate", "unbuffer", "busybox"}) | _KEYWORDS
# A descriptor written against its redirect (`2>&1`, `2>/dev/null`): the lexer would split it off as an argument.
_DESCRIPTOR = re.compile(r"(?:(?<=^)|(?<=[\s;&|(]))\d+(?=[<>])")
# Wrappers whose flags and option values come before the program they run.
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
    _script(command, [], found, 0)
    return found


def shell_heredocs(command: str) -> int:
    """How many heredoc bodies in ``command`` a shell reads from its standard input as its script."""
    return _read(command).fed


def _read(text: str) -> heredocs.Reading:
    try:
        return heredocs.read(text)
    except heredocs.Unreadable as exc:
        raise ShellError(str(exc)) from exc


def _script(text: str, wrappers: list[str], found: list[Invocation], depth: int) -> None:
    """A script a shell reads afresh: the call's command, a `sh -c` script, a heredoc a shell runs, a backtick's."""
    reading = _read(text)
    _walk(reading.top, wrappers, found, depth)
    for script in reading.scripts:
        _script(script, [*wrappers, "<<"], found, depth + 1)
    for ran in reading.runs:
        _inner(ran, [*wrappers, "<<", "$()"], found, depth + 1)


def _walk(code: heredocs.Code, wrappers: list[str], found: list[Invocation], depth: int) -> None:
    if depth > _MAX_DEPTH:
        raise ShellError("the command nests shells too deeply to read")
    for inner in code.inner:
        _inner(inner, [*wrappers, "$()"], found, depth + 1)
    for segment in _segments(code.plain):
        _unwrap(segment.argv, segment, wrappers, found, depth)


def _inner(inner: heredocs.Code | str, wrappers: list[str], found: list[Invocation], depth: int) -> None:
    if isinstance(inner, str):
        _script(inner, wrappers, found, depth)
    else:
        _walk(inner, wrappers, found, depth)


@dataclass(slots=True)
class _Segment:
    """One simple command's words, and the files its redirects write and read."""

    argv: list[str] = field(default_factory=list)
    writes: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)


def _segments(command: str) -> list[_Segment]:
    segments = [_Segment()]
    pending = ""
    for token, operator in _tokens(_DESCRIPTOR.sub("", command)):
        if pending:
            if pending == ">":
                segments[-1].writes.append(token)
            elif pending == "<":
                segments[-1].reads.append(token)
            pending = ""
        elif not operator:
            segments[-1].argv.append(token)
        elif token in _REDIRECTS:
            pending = ">"
        elif token == "<":
            pending = "<"
        elif token in _READS or token == "<<":
            pending = "-"  # a here-string, a heredoc's delimiter or a descriptor: no file
        else:
            segments.append(_Segment())
    return [s for s in segments if s.argv or s.writes or s.reads]


def _tokens(text: str) -> list[tuple[str, bool]]:
    """The words of commands with their quotes removed, and the operators between them (marked True): a run of
    `( ) ; < > | &` outside quotes, which no quoted word can pass for."""
    tokens: list[tuple[str, bool]] = []
    word: list[str] | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        end = i + 1
        if ch in " \t\n" or ch in _PUNCTUATION:
            if word is not None:
                tokens.append(("".join(word), False))
            word = None
            while ch in _PUNCTUATION and end < len(text) and text[end] in _PUNCTUATION:
                end += 1
            if ch in _PUNCTUATION:
                tokens.append((text[i:end], True))
        else:
            word = [] if word is None else word
            if ch in "'\"":
                end = _closing_quote(text, i)
                part = text[i + 1 : end - 1]
                word.append(part if ch == "'" else _DOUBLE_QUOTE_ESCAPE.sub(r"\1", part))
            else:
                end = i + (2 if ch == "\\" else 1)
                word.append(text[end - 1 : end] or ch)  # a backslash that ends the text is itself
        i = end
    return [*tokens, ("".join(word), False)] if word is not None else tokens


def _closing_quote(text: str, start: int) -> int:
    """Where the quote opened at ``start`` ends, just past it; in double quotes a backslash escapes."""
    i = start + 1
    while i < len(text) and text[i] != text[start]:
        i += 2 if text[start] == '"' and text[i] == "\\" else 1
    if i >= len(text):
        raise ShellError("the command cannot be read: a quote is never closed")
    return i + 1


def _unwrap(
    argv: list[str],
    redirects: _Segment,
    wrappers: list[str],
    found: list[Invocation],
    depth: int,
    assigned: tuple[str, ...] = (),
) -> None:
    while argv and _ASSIGNMENT.match(argv[0]):
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
        _script(script, [*wrappers, program], found, depth + 1)


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
                _script(value, [*wrappers, "env"], found, depth + 1)
        elif flag.startswith("--split-string="):
            _script(flag.partition("=")[2], [*wrappers, "env"], found, depth + 1)
        elif flag.startswith("-S") and len(flag) > 2:
            _script(flag[2:], [*wrappers, "env"], found, depth + 1)
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
