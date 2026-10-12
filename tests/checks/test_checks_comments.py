"""What the comment budgets and the history check count as a comment line in TypeScript and JavaScript."""

from __future__ import annotations

import pytest

from checks.runner import comments


@pytest.mark.parametrize(
    ("source", "lines"),
    [
        ("const a = 1; // note\n", {1}),
        ('const s = "// not a comment";\n', set()),
        ('s = "a\\" // b";\n// c\n', {2}),
        ("t = `a ${ b ? '//' : `x` } c`; // real\n", {1}),
        ("t = `${ {a: 1} } // x`;\n// y\n", {2}),
        ("const r = /\\/\\/[/]x/; // after\n", {1}),
        ('const x = a / 2, y = "/"; // c\n', {1}),
        ("x = (/ab\ny) // c\n", {2}),
        ("/* a\n\n b */\ncode();\n", {1, 3}),
    ],
    ids=["trailing", "in-a-string", "escaped-quote", "template-and-nested", "brace-in-interpolation", "regex",
         "division", "regex-cut-by-a-newline", "block-over-a-blank-line"],
)  # fmt: skip
def test_only_a_real_comment_token_counts(source: str, lines: set[int]) -> None:
    assert comments.ts_comment_lines(source) == lines


@pytest.mark.parametrize(
    ("source", "span"),
    [
        ("// one\n// two\nconst a = 1;\n", {1, 2}),
        ("const a = 1;\n// later\n", set()),
        ('const a = "x"; // trailing\nconst b = 2;\n', set()),
    ],
    ids=["leading-run", "after-code", "trailing-on-code"],
)
def test_the_module_docblock_is_the_leading_comment_run(source: str, span: set[int]) -> None:
    assert comments.module_docblock_span("page.ts", source) == span
