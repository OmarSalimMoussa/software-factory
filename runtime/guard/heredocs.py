"""A command read the way bash reads it before it runs: line joins, comments, quotes, substitutions and heredocs, so
no line of a command hides from the guard as a comment, a body or a quoted string.

A backslash before a line break joins the lines, except in single quotes, a comment or a body under a quoted delimiter.
A `#` starts a comment only where a word starts, outside quotes and brackets, and the comment ends at the line break.
A heredoc's body starts after the line break that ends its command line, in the same `$( … )`; under an unquoted
delimiter bash joins its lines before it looks for the delimiter, and still runs each `$( … )` and backtick command in
it. What bash would read in a way the guard does not follow is unreadable. Stdlib only.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field

from runtime.guard import nested

# What stands for a substitution's output in the words the guard reads: no rule may take it for a program or a flag.
EXPANDED = "$()"
_WORD_ENDS = frozenset(" \t\n;&|()<>")
_JOIN = "\\\n"
_UNESCAPE = re.compile(r"\\([\\`$])")
# The escapes of `$'…'` bash decodes to one character; a numbered or control escape is unreadable, any other is text.
_ANSI = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "v": "\v",
         "\\": "\\", "'": "'", '"': '"', "?": "?"}  # fmt: skip
_ARITHMETIC_WORD_ENDS = frozenset(" \t\n()+-*/%<>=!&|^~?:,;")


class Unreadable(ValueError):
    """What the guard cannot read as bash would: a quote, bracket or heredoc left open, or an end it cannot place."""


@dataclass(frozen=True, slots=True)
class Code:
    """Commands as bash runs them: their text, each substitution in it as `EXPANDED`, and what the substitutions run,
    already read, or as a backtick's text to read afresh."""

    plain: str
    inner: tuple[Code | str, ...]


@dataclass(frozen=True, slots=True)
class Reading:
    """A command as bash reads it: ``code`` is its text without heredoc bodies, comments or line joins, ``at`` where
    each character of it stands in the command, and ``cuts`` the `;`, `&&`, `||` and line breaks that cut its list."""

    top: Code
    code: str
    at: tuple[int, ...]
    cuts: tuple[tuple[int, int], ...]
    scripts: tuple[str, ...]  # heredoc bodies a shell reads as its script
    runs: tuple[Code | str, ...]  # what bash runs inside the other bodies, under an unquoted delimiter
    fed: int  # how many bodies a shell reads from its standard input


@functools.lru_cache(maxsize=16)
def read(command: str) -> Reading:
    reader = _Reader(command)
    reader.commands(closer=False)
    reader.closed()
    top = Code("".join(reader.frames[0].plain), tuple(reader.frames[0].inner))
    return Reading(top, "".join(reader.code), tuple(reader.at), tuple(reader.cuts), tuple(reader.scripts),
                   tuple(reader.runs), reader.fed)  # fmt: skip


@dataclass(frozen=True, slots=True)
class _Marker:
    delimiter: str
    quoted: bool
    strip_tabs: bool
    start: int
    line: int


@dataclass(slots=True)
class _Frame:
    """The commands of one level: the call's own, or a `$( … )`'s, with the heredocs opened on its current line."""

    plain: list[str] = field(default_factory=list)
    inner: list[Code | str] = field(default_factory=list)
    pending: list[_Marker] = field(default_factory=list)
    quotes: int = 0


class _Reader:
    def __init__(self, text: str) -> None:
        self.text, self.i = text, 0
        self.code: list[str] = []
        self.at: list[int] = []
        self.cuts: list[tuple[int, int]] = []
        self.frames = [_Frame()]
        self.line = 0
        self.scripts: list[str] = []
        self.runs: list[Code | str] = []
        self.fed = 0

    def _ahead(self, n: int) -> str:
        """The next ``n`` characters as bash reads them, with joined line breaks dropped."""
        got: list[str] = []
        j = self.i
        while len(got) < n and j < len(self.text):
            if self.text.startswith(_JOIN, j):
                j += 2
            else:
                got.append(self.text[j])
                j += 1
        return "".join(got)

    def _joins(self) -> None:
        while self.text.startswith(_JOIN, self.i):
            self.i += 2

    def _take(self, n: int = 1, *, plain: bool = True, joined: bool = True) -> None:
        """Copy the next ``n`` characters into the code, and into the frame's text when ``plain``, where a line break
        outside quotes ends a command as `;` does."""
        for _ in range(n):
            if joined:
                self._joins()
            if self.i >= len(self.text):
                return
            ch = self.text[self.i]
            self.code.append(ch)
            self.at.append(self.i)
            if plain:
                self.frames[-1].plain.append(" ; " if ch == "\n" and not self.frames[-1].quotes else ch)
            self.i += 1

    def _end(self, what: str) -> None:
        self._joins()
        if self.i >= len(self.text):
            raise Unreadable(f"{what} is never closed")

    def commands(self, closer: bool) -> None:
        """Commands up to the end of the text, or to the `)` that closes the `$(` they are in (``closer``)."""
        depth, start = 0, True
        while True:
            self._joins()
            if self.i >= len(self.text):
                if closer:
                    raise Unreadable("a `$(` is never closed")
                return
            ch = self.text[self.i]
            if start and self._comment(closer):
                continue
            if ch == ")" and closer and depth == 0:
                return
            start = ch in _WORD_ENDS
            depth = self._piece(ch, depth)

    def _comment(self, closer: bool) -> bool:
        """At a word's start: read past a comment, if one starts here."""
        if self.text[self.i] == "#":
            end = self.text.find("\n", self.i)
            self.i = len(self.text) if end < 0 else end
            return True
        # A `case` pattern's `)` would close the `$(` early.
        if closer and self._ahead(4) == "case" and self._ahead(5)[4:] in ("", *_WORD_ENDS):
            raise Unreadable("a `case` inside `$( … )` cannot be read; run it plainly")
        return False

    def _piece(self, ch: str, depth: int) -> int:
        """Read one piece of commands at ``depth`` subshells deep: how deep the next piece is."""
        if ch == "\n":
            self._line_break(depth)
        elif self._ahead(2) == "((":
            self._arithmetic(command=True)
        elif ch in "()":
            depth += 1 if ch == "(" else -1
            self._take()
        elif ch in ";&|":
            size = 2 if self._ahead(2) in ("&&", "||") else 1
            at = len(self.code)
            self._take(size)
            if len(self.frames) == 1 and depth == 0 and (size == 2 or ch == ";"):
                self.cuts.append((at, len(self.code)))
        elif self._ahead(2) == "<<" and self._ahead(3) != "<<<":
            self._heredoc()
        else:
            self._word(ch, ansi=True)
        return depth

    def _word(self, ch: str, *, ansi: bool) -> None:
        """One piece of a word outside double quotes: a quote, an escape, an expansion or a character."""
        if ch == "\\":
            self._take(2, joined=False)
        elif ch == "'":
            self._single()
        elif ch == '"':
            self._double()
        elif ch == "`":
            self._backtick()
        elif ch == "$":
            self._dollar(ansi=ansi)
        else:
            self._take()

    def _single(self) -> None:
        end = self.text.find("'", self.i + 1)
        if end < 0:
            raise Unreadable("a quote is never closed")
        self.frames[-1].quotes += 1
        self._take(end + 1 - self.i, joined=False)
        self.frames[-1].quotes -= 1

    def _double(self) -> None:
        self._take()
        self.frames[-1].quotes += 1
        while True:
            self._end("a quote")
            ch = self.text[self.i]
            if ch == '"':
                self.frames[-1].quotes -= 1
                self._take()
                return
            if ch == "\\":
                self._take(2, joined=False)
            elif ch == "$":
                self._dollar(ansi=False)
            elif ch == "`":
                self._backtick()
            else:
                self._take()

    def _ansi(self) -> None:
        """`$'…'`: the frame's text holds the value bash decodes from it, single-quoted, for the word reader."""
        self._take(2, plain=False)
        value: list[str] = []
        while self.text[self.i : self.i + 1] != "'":
            pair = self.text[self.i : self.i + 2]
            if not pair or pair == "\\":
                raise Unreadable("a quote is never closed")
            if pair[0] == "\\" and pair[1] in "01234567xuUc":
                raise Unreadable(f"the escape `{pair}` inside `$'…'` cannot be read; write the character itself")
            value.append(_ANSI.get(pair[1], pair) if pair[0] == "\\" else pair[0])
            self._take(2 if pair[0] == "\\" else 1, plain=False, joined=False)
        self._take(plain=False, joined=False)
        self.frames[-1].plain.append("'" + "".join(value).replace("'", "'\\''") + "'")

    def _backtick(self) -> None:
        """A backtick's text runs as a script of its own, once bash removes the backslashes before `\\`, `` ` `` and
        `$`; it ends at the first backtick no backslash escapes, quotes or not."""
        j = self.i + 1
        while j < len(self.text) and self.text[j] != "`":
            j += 2 if self.text[j] == "\\" else 1
        if j >= len(self.text):
            raise Unreadable("a backtick is never closed")
        self.frames[-1].plain.append(EXPANDED)
        self.frames[-1].inner.append(_UNESCAPE.sub(r"\1", self.text[self.i + 1 : j]))
        self._take(j + 1 - self.i, plain=False, joined=False)

    def _dollar(self, *, ansi: bool) -> None:
        after = self._ahead(3)
        if after.startswith("$(("):
            self._arithmetic(command=False)
        elif after.startswith("$("):
            self._take(2, plain=False)
            self.frames[-1].plain.append(EXPANDED)
            self.frames.append(_Frame())
            self.commands(closer=True)
            self._take(plain=False)
            frame = self.frames.pop()
            self.frames[-1].inner.append(Code("".join(frame.plain), tuple(frame.inner)))
            self.frames[-1].pending.extend(frame.pending)
        elif after.startswith(("${", "$[")):
            self._take(2)
            self._bracket("}" if after[1] == "{" else "]")
        elif after.startswith("$'") and ansi:
            self._ansi()
        else:
            self._take()

    def _bracket(self, close: str) -> None:
        """Up to the `}` of a `${`, which nests no braces, or the `]` of a `$[`, which nests brackets."""
        depth = 0
        while True:
            self._end("a `${`" if close == "}" else "a `$[`")
            ch = self.text[self.i]
            if ch == close and depth == 0:
                self._take()
                return
            if close == "]" and ch in "[]":
                depth += 1 if ch == "[" else -1
            self._word(ch, ansi=True)

    def _arithmetic(self, *, command: bool) -> None:
        """`(( … ))` or `$(( … ))`: arithmetic only if the bracket closing its second `(` comes right before its last;
        otherwise bash runs it as a subshell, where a `#` may start a comment, so the guard reads none."""
        if not command:
            self._take(3, plain=False)
            self.frames[-1].plain.append(EXPANDED)
            self.frames.append(_Frame())
        else:
            self._take(2)
        depth, start, arithmetic = 2, True, None
        while depth:
            self._end("a `((`")
            ch = self.text[self.i]
            if ch == "#" and start:
                raise Unreadable("a `#` inside `(( … ))` cannot be read")
            start = ch in _ARITHMETIC_WORD_ENDS
            if ch in "()":
                depth += 1 if ch == "(" else -1
                self._take(plain=command or depth > 0)
                if depth == 1 and arithmetic is None:
                    arithmetic = self._ahead(1) == ")"
            else:
                self._word(ch, ansi=True)
        if not command:
            frame = self.frames.pop()
            if arithmetic:
                self.frames[-1].inner.extend(frame.inner)
            else:
                self.frames[-1].inner.append(Code("(" + "".join(frame.plain), tuple(frame.inner)))

    def _heredoc(self) -> None:
        at = len(self.code)
        self._take(2)
        strip_tabs = self._ahead(1) == "-"
        self._take(int(strip_tabs))
        while self._ahead(1) in (" ", "\t"):
            self._take()
        word, quoted = "", False
        while (ch := self._ahead(1)) and ch not in _WORD_ENDS and (word or ch != "#"):
            self._joins()
            if ch in "'\"":
                end = self.text.find(ch, self.i + 1)
                part = self.text[self.i + 1 : end]
            else:
                end = self.i + (ch == "\\")
                part = self.text[end : end + 1]
            if ch in "$`" or end < 0 or "\n" in part or (ch == '"' and set(part) & set("\\$`")):
                raise Unreadable("a heredoc delimiter the guard cannot read; write it plainly, as 'EOF'")
            word, quoted = word + part, quoted or ch in "'\"\\"
            self._take(end + 1 - self.i, joined=False)
        if word:
            self.frames[-1].pending.append(_Marker(word, quoted, strip_tabs, at, self.line))

    def _line_break(self, depth: int) -> None:
        at = len(self.code)
        self._take()
        if len(self.frames) == 1 and depth == 0:
            self.cuts.append((at, at + 1))
        markers, self.frames[-1].pending = self.frames[-1].pending, []
        for marker in markers:
            body = self._body(marker)
            shell, reads = nested.heredoc_shell("".join(self.code[marker.line : at]), marker.start - marker.line)
            self.fed += int(reads)
            # Under an unquoted delimiter, the shell reads the body as bash expands it: its escapes gone.
            if shell:
                self.scripts.append(body if marker.quoted else _UNESCAPE.sub(r"\1", body))
            if not marker.quoted:
                self._expansions(body)
        self.line = len(self.code)

    def _body(self, marker: _Marker) -> str:
        lines: list[str] = []
        while True:
            if self.i >= len(self.text):
                raise Unreadable(f"a heredoc never closes: no line `{marker.delimiter}`")
            line = self._body_line(marker.quoted)
            if (line.lstrip("\t") if marker.strip_tabs else line) == marker.delimiter:
                return "".join(f"{line}\n" for line in lines)  # each line keeps its break: a backslash may join it
            lines.append(line)

    def _body_line(self, quoted: bool) -> str:
        """One line of a body; under an unquoted delimiter, a backslash before its break joins the next line to it."""
        parts: list[str] = []
        while True:
            end = self.text.find("\n", self.i)
            end = len(self.text) if end < 0 else end
            part = self.text[self.i : end]
            self.i = min(end + 1, len(self.text))
            odd = (len(part) - len(part.rstrip("\\"))) % 2 == 1
            joins = odd and not quoted and end < len(self.text)
            parts.append(part[:-1] if joins else part)
            if not joins:
                return "".join(parts)

    def _expansions(self, body: str) -> None:
        """What bash runs in a body under an unquoted delimiter: each `$( … )` and backtick command in it. Bash reads
        no quote or comment there."""
        sub = _Reader(body)
        while sub.i < len(body):
            if body[sub.i] == "$":
                sub._dollar(ansi=False)
            elif body[sub.i] == "`":
                sub._backtick()
            else:
                sub._take(2 if body[sub.i] == "\\" else 1, joined=False)
        sub.closed()
        self.runs.extend([*sub.frames[0].inner, *sub.runs])
        self.scripts.extend(sub.scripts)
        self.fed += sub.fed

    def closed(self) -> None:
        if self.frames[0].pending:
            raise Unreadable(f"a heredoc never closes: no line `{self.frames[0].pending[0].delimiter}`")
