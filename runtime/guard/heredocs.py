"""Heredocs, read the way bash reads them, so no line of a command hides from the guard as a body.

A `<<` counts only outside quotes and comments; a `$( … )` starts quoting afresh, so the `"$(cat <<'EOF' …)"` of a
commit message is a heredoc. A delimiter is unquoted as bash does (`E"OF"` ends at `EOF`), and a body that never closes
cannot be read. Stdlib only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from runtime.guard import nested

# Words that run the command after them, read past to find the program a heredoc feeds.
_LEADERS = frozenset({"sudo", "env", "exec", "command", "nohup", "time", "busybox", "nice", "setsid"})
# Where a pipeline ends: `;`, `&&`, `||` or a lone `&`, never the `&` of a redirect (`2>&1`, `&>`).
_PIPELINE_END = re.compile(r"&&|\|\||;|(?<![<>&])&(?![>&])")


class Unclosed(ValueError):
    """A heredoc whose delimiter line never comes."""


@dataclass(frozen=True, slots=True)
class _Marker:
    delimiter: str
    quoted: bool
    strip_tabs: bool
    start: int


@dataclass(slots=True)
class _Frame:
    """One level of quoting: the command itself, a `$( … )` inside it, or a place where `<<` opens no heredoc: an
    arithmetic `(( … ))`, `$(( … ))` or `$[ … ]`, or a `${ … }` expansion."""

    quote: str = ""
    parens: int = 0
    arithmetic: bool = False
    closer: str = ""


# What opens a frame; the longest spelling first.
_OPENERS = ("$((", "$(", "${", "$[")


def _frame_for(opener: str) -> _Frame:
    if opener == "$((":
        return _Frame(parens=1, arithmetic=True)
    if opener == "$(":
        return _Frame()
    return _Frame(closer="}" if opener == "${" else "]")


# A `#` starts a comment where a word starts: after a blank or a metacharacter.
_WORD_START = " \t;&|()<>"


def split(command: str) -> tuple[str, list[str], list[str], int]:
    """The command with every heredoc body cut out, the bodies a shell may run, the other unquoted bodies, and how
    many bodies a shell reads from its standard input.

    A body is text for its program, except a body in a pipeline that runs a shell: that is read as a script. In a body
    under an unquoted delimiter, bash still runs each `$( … )` and backtick command.
    """
    lines = command.split("\n")
    kept: list[str] = []
    scripts: list[str] = []
    expanded: list[str] = []
    frames = [_Frame()]
    fed = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        kept.append(line)
        for marker in _opened(line, frames):
            body: list[str] = []
            while True:
                if i >= len(lines):
                    raise Unclosed(f"a heredoc never closes: no line `{marker.delimiter}`")
                text = lines[i]
                i += 1
                if (text.lstrip("\t") if marker.strip_tabs else text) == marker.delimiter:
                    break
                body.append(text)
            shell, reads = _pipeline_shell(line, marker.start)
            fed += int(reads)
            if shell:
                scripts.append("\n".join(body))
            elif not marker.quoted:
                expanded.append("\n".join(body))
    return "\n".join(kept), scripts, expanded, fed


def _opened(line: str, frames: list[_Frame]) -> list[_Marker]:
    """The heredocs a line opens; ``frames`` carries the quoting into the next line."""
    markers: list[_Marker] = []
    i = 0
    while i < len(line):
        i, marker = _step(line, i, frames)
        if marker is not None:
            markers.append(marker)
    return markers


def _step(line: str, i: int, frames: list[_Frame]) -> tuple[int, _Marker | None]:
    """Read one character or construct at ``i``: where reading goes on, and the heredoc it opened, if any."""
    frame, ch = frames[-1], line[i]
    if frame.closer:
        return _in_expansion(line, i, frames)
    if frame.quote == "'":
        frame.quote = "" if ch == "'" else frame.quote
        return i + 1, None
    if ch == "\\":
        return i + 2, None
    for opener in _OPENERS:
        if line.startswith(opener, i):
            frames.append(_frame_for(opener))
            return i + len(opener), None
    if frame.quote == '"':
        frame.quote = "" if ch == '"' else frame.quote
        return i + 1, None
    return _unquoted(line, i, frames)


def _unquoted(line: str, i: int, frames: list[_Frame]) -> tuple[int, _Marker | None]:
    frame, ch = frames[-1], line[i]
    if ch == "#" and (i == 0 or line[i - 1] in _WORD_START):
        return len(line), None  # the rest of the line is a comment
    if line.startswith("<<", i) and not line.startswith("<<<", i) and not frame.arithmetic:
        marker, end = _delimiter(line, i)
        return end, marker
    if ch in "'\"":
        frame.quote = ch
    elif line.startswith("((", i):
        frames.append(_Frame(parens=1, arithmetic=True))
        return i + 2, None
    elif ch in "()":
        _paren(ch, frames)
    elif line.startswith("<<<", i):
        return i + 3, None
    return i + 1, None


def _in_expansion(line: str, i: int, frames: list[_Frame]) -> tuple[int, None]:
    """Inside `${ … }` or `$[ … ]`, which nest, until the bracket that closes it."""
    frame, ch = frames[-1], line[i]
    opener = "{" if frame.closer == "}" else "["
    if ch == "\\":
        return i + 2, None
    if ch == opener:
        frame.parens += 1
    elif ch == frame.closer:
        if frame.parens:
            frame.parens -= 1
        else:
            frames.pop()
    return i + 1, None


def _paren(ch: str, frames: list[_Frame]) -> None:
    """A bracket opens or closes within the frame, and a closing one past its own ends the `$( … )`."""
    frame = frames[-1]
    if ch == "(":
        frame.parens += 1
    elif frame.parens:
        frame.parens -= 1
    elif len(frames) > 1:
        frames.pop()


def _delimiter(line: str, start: int) -> tuple[_Marker | None, int]:
    i = start + 2
    strip_tabs = line.startswith("-", i)
    i += 1 if strip_tabs else 0
    while i < len(line) and line[i] in " \t":
        i += 1
    word_start = i
    while i < len(line) and line[i] not in " \t;&|<>()":
        if line[i] in "'\"":
            close = line.find(line[i], i + 1)
            i = len(line) if close < 0 else close + 1
        else:
            i += 2 if line[i] == "\\" else 1
    raw = line[word_start:i]
    delimiter = raw.replace("'", "").replace('"', "").replace("\\", "")
    if not delimiter:
        return None, i
    return _Marker(delimiter, delimiter != raw, strip_tabs, start), i


def _pipeline_shell(line: str, start: int) -> tuple[bool, bool]:
    """Whether the pipeline holding the heredoc at ``start`` runs a shell, and whether that shell reads the body from
    its standard input. A group (`{ …; } <<EOF`) may run one, so its body is read, though no shell is known to take it.
    """
    bounds = [0, *(m.end() for m in _PIPELINE_END.finditer(line))]
    ends = [m.start() for m in _PIPELINE_END.finditer(line) if m.start() > start]
    pipeline = line[max(b for b in bounds if b <= start) : min(ends) if ends else len(line)]
    shell = reads = False
    for stage in pipeline.split("|"):
        words = _command_words(stage)
        if words[:1] in (["}"], [")"]):
            shell = True
        elif words and words[0].rsplit("/", 1)[-1] in nested.SHELLS:
            shell = True
            reads = reads or nested.reads_its_script_from_stdin(words[0].rsplit("/", 1)[-1], words[1:])
    return shell, reads


_REDIRECT = re.compile(r"^\d*(<<-?|<<<|<&|>&|&>>?|>>|>\||<|>)")


def _command_words(stage: str) -> list[str]:
    """A stage's words from its program on: past redirects (`<<'A' bash`), assignments, `{`, `!` and leaders."""
    words: list[str] = []
    skip = False
    for word in stage.split():
        if skip:
            skip = False
        elif match := _REDIRECT.match(word):
            skip = match.end() == len(word)  # the redirect's target is the next word
        elif not words and ("=" in word or word in {"{", "(", "!"}):
            continue
        else:
            words.append(word)
    while words and words[0].rsplit("/", 1)[-1] in _LEADERS:
        words = [w for w in words[1:] if not w.startswith("-")]
    return words
