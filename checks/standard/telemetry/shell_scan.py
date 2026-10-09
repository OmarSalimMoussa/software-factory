"""Shell scripts and Dockerfile `RUN` lines to facts, with a line scanner.

Masks comments, quoted text and heredoc bodies (a `$(...)` inside double
quotes stays code), joins `\\`-continued lines, then finds `shell-swallow`
(`|| true`, `|| :`, `set +e`), `shell-hidden` (a stderr discard on a command
whose exit status nothing reads) and `shell-strict` (an executed script
without `set -e`, `-u` and `-o pipefail` before its first command). When
unsure it does not flag. A `trap` line's quoted handler is read unmasked.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Collection, Iterator

from checks.standard.telemetry.facts import MARKER_RE, CatchSite, FileFacts, MarkerComment, SiteKind

_SWALLOW = re.compile(r"\|\|\s*(true|:)(?=[\s;)}'\"]|$)|\bset\s+\+[a-zA-Z]*e")
_HIDDEN = re.compile(r"(?<![\w&])2>\s*/dev/null|&>\s*/dev/null|>\s*/dev/null\s+2>&1")
_STATUS_READ = re.compile(r"&&|\|\||\||\$\(|^\s*(if|elif|while|until|!)\s")
_HEREDOC = re.compile(r"<<-?\s*['\"]?([A-Za-z_]\w*)['\"]?")
_COMMENT_START = frozenset(" \t;|&(")


class _Masker:
    def __init__(self) -> None:
        self.stack = ["code"]
        self.comment_at: int | None = None

    def _code(self, line: str, i: int, out: list[str]) -> int:
        char, nxt = line[i], line[i + 1 : i + 2]
        if char == "#" and (i == 0 or line[i - 1] in _COMMENT_START):
            self.comment_at = i
            out.append(" " * (len(line) - i))
            return len(line)
        if char == "\\":
            out.append(line[i : i + 2])
            return i + 2
        if char in "'\"":
            self.stack.append("sq" if char == "'" else "dq")
            out.append(" ")
            return i + 1
        if char == "$" and nxt == "(":
            self.stack.append("subst")
            out.append("$(")
            return i + 2
        if char == "(":
            self.stack.append("paren")
        elif char == ")" and self.stack[-1] in ("subst", "paren"):
            self.stack.pop()
        out.append(char)
        return i + 1

    def _quoted(self, line: str, i: int, out: list[str]) -> int:
        char, top = line[i], self.stack[-1]
        if top == "dq" and char == "\\":
            out.append("  "[: len(line[i : i + 2])])
            return i + 2
        if top == "dq" and char == "$" and line[i + 1 : i + 2] == "(":
            self.stack.append("subst")
            out.append("$(")
            return i + 2
        if char == ("'" if top == "sq" else '"'):
            self.stack.pop()
        out.append(" ")
        return i + 1

    def line(self, line: str) -> str:
        out: list[str] = []
        self.comment_at = None
        i = 0
        while i < len(line):
            step = self._quoted if self.stack[-1] in ("sq", "dq") else self._code
            i = step(line, i, out)
        return "".join(out)


@dataclasses.dataclass
class Masked:
    """Masked lines, the numbers of lines that end inside a quote or `$(`,
    and the column each comment starts at, by line number."""

    lines: list[str]
    open_lines: set[int]
    comments: dict[int, int]


def mask(text: str) -> Masked:
    masker = _Masker()
    result = Masked([], set(), {})
    terminator: tuple[str, bool] | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        if terminator is not None:
            word, dash = terminator
            if (raw.strip() if dash else raw) == word:
                terminator = None
            result.lines.append("")
            continue
        line = masker.line(raw)
        result.lines.append(line)
        if masker.comment_at is not None:
            result.comments[number] = masker.comment_at
        if masker.stack[-1] in ("sq", "dq", "subst"):
            result.open_lines.add(number)
        heredoc = _HEREDOC.search(raw)
        if heredoc and "<<" in line and "<<<" not in line:
            dash = raw[heredoc.start() + 2 : heredoc.start() + 3] == "-"
            terminator = (heredoc.group(1), dash)
    return result


def logical_lines(
    lines: list[str], open_lines: Collection[int] = frozenset(), first: int = 1
) -> Iterator[tuple[str, list[tuple[int, int]]]]:
    """Joined `\\`-continued and quote-spanning lines, each with its
    (offset, physical line) spans."""
    text, spans = "", []
    for number, line in enumerate(lines, first):
        body = line.rstrip()
        slashed = body.endswith("\\")
        spans.append((len(text), number))
        text += (body[:-1] if slashed else body) + " "
        if not slashed and number not in open_lines:
            yield text, spans
            text, spans = "", []
    if spans:
        yield text, spans


def _line_at(spans: list[tuple[int, int]], offset: int) -> int:
    return max((n for start, n in spans if start <= offset), default=spans[0][1])


def _raw_logical(raw: list[str], spans: list[tuple[int, int]]) -> str:
    return " ".join(raw[n - 1].rstrip().rstrip("\\") for _, n in spans)


def _status_read(joined: str, following: str) -> bool:
    """The exit status of JOINED is read: it is tested, chained, piped or
    substituted, or it is a function's last command (FOLLOWING closes the
    body), whose status the caller reads."""
    return bool(_STATUS_READ.search(joined)) or following.strip().startswith("}")


def _sites_in(path: str, text: str, masked: Masked) -> list[CatchSite]:
    raw = text.splitlines()
    sites: list[CatchSite] = []

    def add(kind: SiteKind, line: int, detail: str) -> None:
        if not any(s.kind == kind and s.line == line for s in sites):
            sites.append(CatchSite(path, line, kind, detail, line + 1, scope_text=text))

    logical = [item for item in logical_lines(masked.lines, masked.open_lines) if item[0].strip()]
    for index, (joined, spans) in enumerate(logical):
        is_trap = joined.lstrip().startswith("trap ")
        swallow_text = _raw_logical(raw, spans) if is_trap else joined
        for match in _SWALLOW.finditer(swallow_text):
            line = spans[0][1] if is_trap else _line_at(spans, match.start())
            add("shell-swallow", line, match.group(0).strip())
        hidden = _HIDDEN.search(joined)
        following = logical[index + 1][0] if index + 1 < len(logical) else ""
        if hidden and not _status_read(joined, following):
            add("shell-hidden", _line_at(spans, hidden.start()), hidden.group(0))
    return sites


_STRICT = (("e", "-e"), ("u", "-u"), ("pipefail", "-o pipefail"))
_LONG_FLAGS = {"errexit": "e", "nounset": "u"}


def _strict_site(path: str, text: str, masked: Masked) -> CatchSite | None:
    if not text.startswith("#!"):
        return None
    flags: set[str] = set()
    for joined, _ in logical_lines(masked.lines[1:], masked.open_lines, first=2):
        words = joined.split()
        if not words:
            continue
        if words[0] != "set":
            break
        for word in words[1:]:
            short = word[1:] if word.startswith("-") else ""
            flags |= set(short) if short else {_LONG_FLAGS.get(word, word)}
    missing = [name for flag, name in _STRICT if flag not in flags]
    if not missing:
        return None
    detail = f"no {', '.join(missing)} before the first command"
    return CatchSite(path, 1, "shell-strict", detail, 2, scope_text=text)


def _marker(path: str, number: int, comment: str) -> MarkerComment | None:
    body = comment.lstrip("#").strip()
    return MarkerComment(path, number, body) if MARKER_RE.match(body) else None


def scan_script(path: str, text: str) -> FileFacts:
    masked = mask(text)
    sites = _sites_in(path, text, masked)
    strict = _strict_site(path, text, masked)
    if strict is not None:
        sites.insert(0, strict)
    raw = text.splitlines()
    found = (_marker(path, n, raw[n - 1][col:]) for n, col in masked.comments.items())
    markers = tuple(m for m in found if m is not None)
    return FileFacts(path, sites=tuple(sites), markers=markers)


def _run_script(lines: list[str]) -> str:
    """The Dockerfile's shell-form `RUN` text, one output line per input line
    so line numbers hold; every other line blank, and a comment inside a
    continuation kept as a bare `\\` so the join carries on."""
    out: list[str] = []
    in_run = False
    for line in lines:
        stripped = line.strip()
        if in_run and stripped.startswith("#"):
            out.append("\\")
            continue
        starts = re.match(r"^\s*RUN\s+(?!\[)", line, re.I)
        if starts:
            in_run = True
            out.append(" " * starts.end() + line[starts.end() :])
        elif in_run:
            out.append(line)
        else:
            out.append("")
        if in_run and not stripped.endswith("\\"):
            in_run = False
    return "\n".join(out)


def scan_dockerfile(path: str, text: str) -> FileFacts:
    """A Dockerfile's `RUN` sites; its markers are whole-line `#` comments,
    since Docker passes a trailing `#` on to the shell."""
    script = _run_script(text.splitlines())
    sites = tuple(dataclasses.replace(site, scope_text=text) for site in _sites_in(path, script, mask(script)))
    found = (
        _marker(path, n, line.strip()) for n, line in enumerate(text.splitlines(), 1) if line.lstrip().startswith("#")
    )
    return FileFacts(path, sites=sites, markers=tuple(m for m in found if m is not None))
