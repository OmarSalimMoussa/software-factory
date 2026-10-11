"""A reader for the block-style YAML that compose files, workflows and app configs are written in.

It reads block mappings and sequences, plain, quoted and block scalars, one-line flow collections, anchors, aliases
and merge keys (``<<``), with YAML 1.2 core scalars. Anything else (tags, complex keys, several documents, a flow
collection spanning lines) raises YamlError naming the line, so a file it cannot read is a problem, never a guess.
"""

from __future__ import annotations

import re
from typing import Any


class YamlError(ValueError):
    """The text is outside the subset this reader takes; the message names the line."""


_INT_RE = re.compile(r"^[-+]?(0|[1-9][0-9]*)$")
_FLOAT_RE = re.compile(r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$")
_ESCAPES = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t", "r": "\r", "0": "\0", " ": " "}


def _strip_comment(line: str) -> str:
    quote = ""
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\" and quote == '"':
                index += 1
            elif char == quote:
                quote = ""
        elif char in "'\"" and (index == 0 or line[index - 1] in " \t:-[{,"):
            quote = char
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index].rstrip()
        index += 1
    return line.rstrip()


def _unquote(text: str, number: int) -> tuple[str, str]:
    """The quoted scalar at the start of ``text`` and what follows it."""
    quote, out, index = text[0], [], 1
    while index < len(text):
        char = text[index]
        if quote == "'" and char == "'":
            if text[index + 1 : index + 2] == "'":
                out.append("'")
                index += 2
                continue
            return "".join(out), text[index + 1 :]
        if quote == '"' and char == "\\":
            escaped = text[index + 1 : index + 2]
            if escaped not in _ESCAPES:
                raise YamlError(f"line {number}: the escape \\{escaped} is not read")
            out.append(_ESCAPES[escaped])
            index += 2
            continue
        if quote == '"' and char == '"':
            return "".join(out), text[index + 1 :]
        out.append(char)
        index += 1
    raise YamlError(f"line {number}: a quoted scalar does not close on its line")


def scalar(text: str, number: int) -> Any:
    if text[:1] in ("'", '"'):
        value, rest = _unquote(text, number)
        if rest.strip():
            raise YamlError(f"line {number}: text after a quoted scalar: {rest.strip()!r}")
        return value
    if text in ("null", "Null", "NULL", "~"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(text):
        return int(text)
    if text.startswith(("0x", "0o")):
        try:
            return int(text, 0)
        # telemetry: expected — a 0x or 0o word that is no number is a string in YAML, as it reads
        except ValueError:
            return text
    if _FLOAT_RE.match(text):
        return float(text)
    return text


def _split_key(content: str, number: int) -> tuple[str, str] | None:
    """A mapping entry's key and the text after its colon, or None when ``content`` is no mapping entry."""
    if content[:1] in "[{&*!|>" or content.startswith("? "):
        if content.startswith("? "):
            raise YamlError(f"line {number}: complex keys are not read")
        return None
    if content[:1] in ("'", '"'):
        key, rest = _unquote(content, number)
        if rest.startswith(":") and (len(rest) == 1 or rest[1] in " \t"):
            return key, rest[1:]
        return None
    for index, char in enumerate(content):
        if char == ":" and (index + 1 == len(content) or content[index + 1] in " \t"):
            return content[:index].rstrip(), content[index + 1 :]
    return None


class _Flow:
    def __init__(self, text: str, number: int) -> None:
        self.text, self.at, self.number = text, 0, number

    def _space(self) -> None:
        while self.at < len(self.text) and self.text[self.at] in " \t":
            self.at += 1

    def node(self) -> Any:
        self._space()
        if self.at >= len(self.text):
            raise YamlError(f"line {self.number}: a flow collection does not close on its line")
        opener = self.text[self.at]
        if opener in "[{":
            self.at += 1
            closer = "]" if opener == "[" else "}"
            items: list[Any] = []
            table: dict[str, Any] = {}
            while True:
                self._space()
                if self.text[self.at : self.at + 1] == closer:
                    self.at += 1
                    return items if opener == "[" else table
                item = self.node()
                self._space()
                if opener == "{":
                    if self.text[self.at : self.at + 1] != ":":
                        raise YamlError(f"line {self.number}: a flow mapping entry needs a colon")
                    self.at += 1
                    table[str(item)] = self.node()
                else:
                    items.append(item)
                self._space()
                if self.text[self.at : self.at + 1] == ",":
                    self.at += 1
                elif self.text[self.at : self.at + 1] != closer:
                    raise YamlError(f"line {self.number}: a flow collection does not close on its line")
        if opener in ("'", '"'):
            value, rest = _unquote(self.text[self.at :], self.number)
            self.at = len(self.text) - len(rest)
            return value
        start = self.at
        while (
            self.at < len(self.text)
            and self.text[self.at] not in ",]}"
            and not (self.text[self.at] == ":" and self.text[self.at + 1 : self.at + 2] in (" ", ""))
        ):
            self.at += 1
        return scalar(self.text[start : self.at].strip(), self.number)


class _Parser:
    def __init__(self, text: str) -> None:
        self.raw = text.splitlines()
        self.at = 0
        self.anchors: dict[str, Any] = {}
        self.override: dict[int, tuple[int, str]] = {}

    def line(self, index: int) -> tuple[int, str]:
        if index in self.override:
            return self.override[index]
        raw = self.raw[index]
        content = _strip_comment(raw)
        indent = len(content) - len(content.lstrip(" "))
        if content[indent:indent + 1] == "\t":  # fmt: skip
            raise YamlError(f"line {index + 1}: a tab indents it")
        return indent, content.strip()

    def peek(self) -> tuple[int, str] | None:
        while self.at < len(self.raw):
            indent, content = self.line(self.at)
            if content == "...":
                self.at = len(self.raw)
                return None
            if content:
                return indent, content
            self.at += 1
        return None

    def document(self) -> Any:
        first = self.peek()
        if first is not None and first[1] == "---":
            self.at += 1
        if first is not None and first[1].startswith("%"):
            raise YamlError(f"line {self.at + 1}: directives are not read")
        nxt = self.peek()
        value = None if nxt is None else self.block(nxt[0])
        rest = self.peek()
        if rest is not None:
            what = "several documents are" if rest[1] == "---" else "this line is"
            raise YamlError(f"line {self.at + 1}: {what} not read")
        return value

    def block(self, indent: int) -> Any:
        _, content = self.line(self.at)
        return self.sequence(indent) if content == "-" or content.startswith("- ") else self.mapping(indent)

    def mapping(self, indent: int) -> dict[str, Any]:
        table: dict[str, Any] = {}
        merged: list[Any] = []
        while (nxt := self.peek()) is not None and nxt[0] >= indent:
            number = self.at + 1
            if nxt[0] > indent:
                raise YamlError(f"line {number}: unexpected indentation")
            if nxt[1] == "-" or nxt[1].startswith("- "):
                raise YamlError(f"line {number}: a sequence item where a `key: value` line belongs")
            entry = _split_key(nxt[1], number)
            if entry is None:
                raise YamlError(f"line {number}: expected a `key: value` line")
            self.at += 1
            key, rest = entry
            value = self.value(rest, indent, number)
            if key == "<<":
                merged += value if isinstance(value, list) else [value]
            else:
                table[key] = value
        for source in merged:
            if not isinstance(source, dict):
                raise YamlError("a merge key (<<) names something that is not a mapping")
            for key, value in source.items():
                table.setdefault(key, value)
        return table

    def sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while (nxt := self.peek()) is not None and nxt[0] == indent and (nxt[1] == "-" or nxt[1].startswith("- ")):
            number = self.at + 1
            inner = nxt[1][1:].lstrip(" ")
            nested = inner == "-" or inner.startswith("- ")
            if inner and (nested or _split_key(inner, number) is not None):
                depth = indent + len(nxt[1]) - len(inner)
                self.override[self.at] = (depth, inner)
                items.append(self.sequence(depth) if nested else self.mapping(depth))
                continue
            self.at += 1
            items.append(self.value(inner, indent, number))
        return items

    def value(self, rest: str, indent: int, number: int) -> Any:
        rest = rest.strip()
        anchor = ""
        if rest.startswith("&"):
            anchor, _, rest = rest[1:].partition(" ")
            rest = rest.strip()
        if rest.startswith("!"):
            raise YamlError(f"line {number}: tags are not read")
        if anchor and rest and _split_key(rest, number) is not None:
            raise YamlError(f"line {number}: an anchor before an inline mapping entry is not read")
        if not rest:
            nxt = self.peek()
            if nxt is not None and nxt[0] > indent:
                found = self.block(nxt[0])
            elif nxt is not None and nxt[0] == indent and (nxt[1] == "-" or nxt[1].startswith("- ")):
                found = self.sequence(indent)
            else:
                found = None
        elif rest[0] in "|>":
            found = self.block_scalar(rest, indent, number)
        elif rest.startswith("*"):
            if rest[1:] not in self.anchors:
                raise YamlError(f"line {number}: the alias {rest} names no anchor above it")
            found = self.anchors[rest[1:]]
        elif rest[0] in "[{":
            flow = _Flow(rest, number)
            found = flow.node()
            if rest[flow.at :].strip():
                raise YamlError(f"line {number}: text after a flow collection")
        else:
            found = scalar(rest, number)
        if anchor:
            self.anchors[anchor] = found
        return found

    def block_scalar(self, header: str, indent: int, number: int) -> str:
        if header[1:].strip() not in ("", "-", "+"):
            raise YamlError(f"line {number}: the block scalar header {header!r} is not read")
        body: list[str] = []
        while self.at < len(self.raw):
            raw = self.raw[self.at]
            if raw.strip() and len(raw) - len(raw.lstrip(" ")) <= indent:
                break
            body.append(raw)
            self.at += 1
        while body and not body[-1].strip():
            body.pop()
        depth = min((len(r) - len(r.lstrip(" ")) for r in body if r.strip()), default=0)
        lines = [r[depth:] for r in body]
        text = "\n".join(lines) if header[0] == "|" else " ".join(r.strip() for r in lines if r.strip())
        return text if header.endswith("-") else text + "\n"


def load(text: str) -> Any:
    """The one document in ``text``; raises YamlError for anything outside the subset."""
    return _Parser(text).document()
