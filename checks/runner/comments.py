"""The one definition of a comment line the comment budgets and the history check use.

- Python: `tokenize` finds every `#` comment; `ast` finds every module, class and function docstring, its non-blank
  physical lines only.
- TypeScript/JavaScript: a character-level state machine tracks `//`, `/* ... */`, strings, template literals and
  regex literals (told apart from division by the last significant code character). A token inside a string,
  template or regex is text; a stray delimiter inside a comment is text.

Shell and Rust have no rule: Rust block comments nest and raw strings can hide a bare `//`. A leading licence block
is not part of a module docblock: a block counts as one only if a licence marker names it in its first three
non-blank lines.
"""

from __future__ import annotations

import ast
import functools
import io
import tokenize

TS_SUFFIXES = (".ts", ".tsx", ".js", ".mjs")
_DOCSTRING_HOLDERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
# A license header is not documentation: matched against a block's first three non-blank lines only, so a docblock that
# merely mentions one of these phrases mid-paragraph is unaffected.
_LICENSE_MARKERS = (
    "SPDX-License-Identifier",
    "Licensed under the Apache License",
    "MIT License",
    "Copyright (c)",
)


# One file's text is asked for its tree and its TS scan several times per gate
# pass; a small cache holds the last few files only (a whole repo's ASTs would
# run to hundreds of megabytes). Callers only read what these return.
_PER_FILE_CACHE = 8


@functools.lru_cache(maxsize=_PER_FILE_CACHE)
def _parse(text: str) -> ast.Module:
    return ast.parse(text)


@functools.lru_cache(maxsize=_PER_FILE_CACHE)
def _docstrings(text: str) -> tuple[tuple[bool, ast.Constant], ...]:
    found: list[tuple[bool, ast.Constant]] = []
    for node in ast.walk(_parse(text)):
        if not isinstance(node, _DOCSTRING_HOLDERS):
            continue
        doc = _docstring_node(node)
        if doc is not None:
            found.append((isinstance(node, ast.Module), doc))
    return tuple(found)


def _docstring_node(node: ast.AST) -> ast.Constant | None:
    """The literal node holding NODE's docstring, or None. Mirrors `ast.get_docstring`: only the first statement in the
    body, and only a bare string-literal expression, counts."""
    body = getattr(node, "body", None)
    if not body:
        return None
    first = body[0]
    if not isinstance(first, ast.Expr):
        return None
    value = first.value
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value
    return None


def _nonblank_span(lines: list[str], start: int, end: int) -> set[int]:
    return {ln for ln in range(start, end + 1) if lines[ln - 1].strip()}


def py_comment_lines(text: str) -> set[int]:
    """1-based line numbers holding a `#` comment (never a first-line `#!`, which only tells the kernel how to run the
    file) or a docstring line (module, class, function), each counted once; blank lines inside a docstring are not.
    Raises SyntaxError when the text does not parse."""
    lines = text.splitlines()
    result: set[int] = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                result.add(tok.start[0])
    except tokenize.TokenError as exc:
        raise SyntaxError(exc.args[0]) from exc  # args: the message, then where
    for _, doc in _docstrings(text):
        result |= _nonblank_span(lines, doc.lineno, doc.end_lineno or doc.lineno)
    return result - {1} if text.startswith("#!") else result


def _ends_expression(ch: str) -> bool:
    """True when CH ends a JS/TS expression (identifier character, digit, `)` or `]`), so a following `/` is division,
    not a regex literal's start: the last-significant-token heuristic."""
    return ch.isalnum() or ch in ("_", "$", ")", "]")


@functools.lru_cache(maxsize=_PER_FILE_CACHE)
def _scan_ts(text: str) -> tuple[frozenset[int], frozenset[int]]:
    """Character-level state machine over TS/JS source. Returns (comment_line_numbers, code_line_numbers); a line can
    hold both (a trailing `// comment` after real code). Strings, template literals (`${…}` is nested code) and
    regex literals (`_ends_expression` decides `/` vs division; `[...]` classes do not end one early; one cannot span
    a physical line, so a stray newline mid-body bails back to ordinary code) are opaque: a `//` or `/*` inside is
    text."""
    comment_lines: set[int] = set()
    code_lines: set[int] = set()
    n = len(text)
    i = 0
    line = 1
    submode = "normal"  # normal | line_comment | block_comment
    # ("str", quote) | ("tmpl", None) | ("interp", depth) | ("regex", in_class)
    frames: list[tuple[str, object]] = []
    escaped = False
    last_code_char = ""

    def _mark_code(ch: str) -> None:
        nonlocal last_code_char
        if not ch.isspace():
            code_lines.add(line)
            last_code_char = ch

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        top = frames[-1] if frames else None
        top_kind = top[0] if top else "code"

        if top_kind == "regex" and ch == "\n":
            # Can't span a physical line: bail, drop the frame and reprocess this character as ordinary code.
            frames.pop()
            continue

        if top_kind in ("str", "tmpl") and escaped:
            escaped = False
            if ch == "\n":
                line += 1
            i += 1
            continue

        if ch == "\n":
            line += 1
            i += 1
            if submode == "line_comment":
                submode = "normal"
            continue

        if submode == "line_comment":
            comment_lines.add(line)
            i += 1
            continue

        if submode == "block_comment":
            comment_lines.add(line)
            if ch == "*" and nxt == "/":
                submode = "normal"
                i += 2
            else:
                i += 1
            continue

        if top_kind in ("str", "tmpl"):
            _mark_code(ch)
            if ch == "\\":
                escaped = True
                i += 1
                continue
            if top_kind == "str" and ch == top[1]:  # type: ignore[index]
                frames.pop()
                i += 1
                continue
            if top_kind == "tmpl" and ch == "`":
                frames.pop()
                i += 1
                continue
            if top_kind == "tmpl" and ch == "$" and nxt == "{":
                frames.append(("interp", 0))
                i += 2
                continue
            i += 1
            continue

        if top_kind == "regex":
            in_class = top[1]  # type: ignore[index]
            _mark_code(ch)
            if ch == "\\" and i + 1 < n and text[i + 1] != "\n":
                i += 2  # an escaped char, including \/ and \] — never a delimiter
                continue
            if ch == "[":
                frames[-1] = ("regex", True)
                i += 1
                continue
            if ch == "]" and in_class:
                frames[-1] = ("regex", False)
                i += 1
                continue
            if ch == "/" and not in_class:
                frames.pop()
                last_code_char = ")"  # a regex literal ends an expression
                i += 1
                continue
            i += 1
            continue

        # top_kind == "code": genuinely top-level, or inside a ${...}
        if ch == "/" and nxt == "/":
            submode = "line_comment"
            comment_lines.add(line)
            i += 2
            continue
        if ch == "/" and nxt == "*":
            submode = "block_comment"
            comment_lines.add(line)
            i += 2
            continue
        if ch == "/" and not _ends_expression(last_code_char):
            frames.append(("regex", False))
            code_lines.add(line)
            i += 1
            continue
        if ch in ("'", '"'):
            frames.append(("str", ch))
            _mark_code(ch)
            i += 1
            continue
        if ch == "`":
            frames.append(("tmpl", None))
            _mark_code(ch)
            i += 1
            continue
        if top_kind == "interp":
            depth = top[1]  # type: ignore[index]
            assert isinstance(depth, int)
            if ch == "{":
                frames[-1] = ("interp", depth + 1)
                _mark_code(ch)
                i += 1
                continue
            if ch == "}":
                if depth == 0:
                    frames.pop()
                    i += 1
                    continue
                frames[-1] = ("interp", depth - 1)
                _mark_code(ch)
                i += 1
                continue
        _mark_code(ch)
        i += 1

    return frozenset(comment_lines), frozenset(code_lines)


def ts_comment_lines(text: str) -> set[int]:
    """1-based line numbers holding a `//` or `/* … */` (incl. JSDoc) comment token. A line whose only comment content
    is inside a string or template literal is excluded, as is a blank continuation line inside a block comment."""
    lines = text.splitlines()
    raw, _ = _scan_ts(text)
    return {ln for ln in raw if lines[ln - 1].strip()}


def _is_license_block(lines: list[str], first: int, last: int) -> bool:
    """Whether the block spanning [first, last] is a license header: one of `_LICENSE_MARKERS` appears in its first
    three non-blank physical lines (a later mention is a docblock that happens to mention a copyright)."""
    checked = 0
    for ln in range(first, last + 1):
        content = lines[ln - 1]
        if not content.strip():
            continue
        if any(marker in content for marker in _LICENSE_MARKERS):
            return True
        checked += 1
        if checked >= 3:
            break
    return False


def _split_into_paragraphs(lines: list[str], first: int, last: int) -> list[tuple[int, int]]:
    blocks: list[tuple[int, int]] = []
    ln = first
    while ln <= last:
        if not lines[ln - 1].strip():
            ln += 1
            continue
        start = ln
        end = ln
        while ln <= last and lines[ln - 1].strip():
            end = ln
            ln += 1
        blocks.append((start, end))
    return blocks


def _py_module_docblock_span(text: str) -> set[int]:
    lines = text.splitlines()
    doc = _docstring_node(_parse(text))
    if doc is None:
        return set()
    first, last = doc.lineno, doc.end_lineno or doc.lineno
    paragraphs = _split_into_paragraphs(lines, first, last)
    if paragraphs and _is_license_block(lines, *paragraphs[0]):
        paragraphs = paragraphs[1:]
    span: set[int] = set()
    for a, b in paragraphs:
        span |= _nonblank_span(lines, a, b)
    return span


def _ts_split_into_blocks(lines: list[str], start: int, end: int) -> list[tuple[int, int]]:
    """Split the leading comment-only run [start, end] into blocks: one span per `/* ... */` (naive `*/` search,
    matching `_scan_ts`'s own rule) or contiguous `//` run. A blank line always ends the current block."""
    blocks: list[tuple[int, int]] = []
    ln = start
    while ln <= end:
        if not lines[ln - 1].strip():
            ln += 1
            continue
        block_start = ln
        stripped = lines[ln - 1].lstrip()
        if stripped.startswith("/*"):
            close = ln
            body = lines[ln - 1]
            idx = body.find("/*") + 2
            while "*/" not in body[idx:]:
                close += 1
                if close > end:
                    close = end
                    break
                body = lines[close - 1]
                idx = 0
            blocks.append((block_start, close))
            ln = close + 1
        else:
            close = ln
            ln += 1
            while ln <= end and lines[ln - 1].strip() and lines[ln - 1].lstrip().startswith("//"):
                close = ln
                ln += 1
            blocks.append((block_start, close))
    return blocks


def _ts_module_docblock_span(text: str) -> set[int]:
    lines = text.splitlines()
    comment_lines = ts_comment_lines(text)
    _, code_lines = _scan_ts(text)
    # A `#!` shebang (Node/Bun CLI scripts) is neither TS comment syntax nor part of the block: skip it before looking
    # for the header.
    start = 2 if lines and lines[0].startswith("#!") else 1
    run_end = start - 1
    for ln in range(start, len(lines) + 1):
        if not lines[ln - 1].strip():
            continue  # blank: skip, doesn't break the block, doesn't count
        if ln in code_lines:
            break  # a mixed comment+code line is still a code line
        if ln in comment_lines:
            run_end = ln
        else:
            break
    if run_end < start:
        return set()
    blocks = _ts_split_into_blocks(lines, start, run_end)
    if blocks and _is_license_block(lines, *blocks[0]):
        blocks = blocks[1:]
    span: set[int] = set()
    for first, last in blocks:
        span |= _nonblank_span(lines, first, last)
    return span


def module_docblock_span(path: str, text: str) -> set[int]:
    """Line numbers of the file's leading documentation block: the module docstring's span (Python), or the leading run
    of comment lines before the first real code line (TS/JS). Blanks excluded; a license block is not part of it. The
    density gate subtracts this from the comment count, since the module-docblock ceiling already governs it."""
    if path.endswith(".py"):
        return _py_module_docblock_span(text)
    if path.endswith(TS_SUFFIXES):
        return _ts_module_docblock_span(text)
    raise ValueError(f"comment_counts: no docblock rule for {path!r}")


def docstrings_over(path: str, text: str, threshold: int) -> int:
    """Count of function and class docstrings (module docstrings are the module-docblock budget's) whose non-blank
    span exceeds THRESHOLD lines. Always 0 for TS/JS."""
    if not path.endswith(".py"):
        return 0
    lines = text.splitlines()
    count = 0
    for is_module, doc in _docstrings(text):
        if is_module:
            continue
        span = _nonblank_span(lines, doc.lineno, doc.end_lineno or doc.lineno)
        if len(span) > threshold:
            count += 1
    return count
