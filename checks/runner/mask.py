"""Source text with comments and strings masked out, so a line scanner sees only code.

``js_code`` masks JS/TS character by character, keeping every line and column where it was: a trailing
``// console.log(x)`` note is no call. Named residual: a call inside a template literal's ``${...}`` reads as string
text. ``python_code`` keeps each line's code tokens only.
"""

from __future__ import annotations

import io
import tokenize
from typing import Literal

_Mode = Literal["code", "block", "str", "tmpl"]


class _State:
    def __init__(self) -> None:
        self.mode: _Mode = "code"
        self.quote = ""

    def open(self, mode: _Mode, quote: str) -> None:
        self.mode, self.quote = mode, quote


def _step_opaque(state: _State, char: str, after: str) -> tuple[int, str]:
    if state.mode == "block":
        if char == "*" and after == "/":
            state.open("code", "")
            return 2, "  "
        return 1, " "
    if char == "\\":
        return 2, "  "
    if char == state.quote:
        state.open("code", "")
    return 1, " "


def _step_code(state: _State, char: str, after: str, rest: int) -> tuple[int, str]:
    if char == "/" and after == "/":
        return rest, " " * rest
    if char == "/" and after == "*":
        state.open("block", "")
        return 2, "  "
    if char in ("'", '"', "`"):
        state.open("tmpl" if char == "`" else "str", char)
        return 1, " "
    return 1, char


def js_code(text: str) -> list[str]:
    """``text``'s lines with comment and string characters replaced by spaces. A quoted string ends at the newline;
    a template literal and a block comment carry across lines."""
    masked: list[str] = []
    state = _State()
    for line in text.splitlines():
        out: list[str] = []
        index, width = 0, len(line)
        while index < width:
            char = line[index]
            after = line[index + 1] if index + 1 < width else ""
            if state.mode == "code":
                step, shown = _step_code(state, char, after, width - index)
            else:
                step, shown = _step_opaque(state, char, after)
            out.append(shown)
            index += step
        if state.mode == "str":
            state.open("code", "")
        masked.append("".join(out)[:width])
    return masked


def python_code(text: str) -> dict[int, str]:
    """Each line's code tokens joined by spaces, strings and comments dropped, keyed by line number.
    Raises SyntaxError (tokenize.TokenError's message) when the text does not tokenize."""
    lines: dict[int, list[str]] = {}
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type in (tokenize.STRING, tokenize.COMMENT, tokenize.FSTRING_MIDDLE):
                continue
            lines.setdefault(token.start[0], []).append(token.string)
    except tokenize.TokenError as exc:
        raise SyntaxError(str(exc)) from exc
    return {number: " ".join(parts) for number, parts in lines.items()}
