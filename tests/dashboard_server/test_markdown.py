"""The Library's renderer: nothing in a document reaches the page as markup but what the renderer writes."""

from __future__ import annotations

from runtime.dashboard.markdown import render


def test_everything_is_escaped_and_only_safe_links_are_made() -> None:
    source = (
        '<script>alert(1)</script> <img src=x onerror="alert(1)">\n\n'
        "[bad](javascript:alert(1)) [ok](https://example.org/a?b=1&c=2) `<b>code</b>`\n\n"
        "t-0017 names itself; t-0018 is another task; t-0099 is unknown."
    )
    html = render(source, known={"t-0017", "t-0018"}, own="t-0017")
    assert "<script" not in html and "<img" not in html and "&lt;script&gt;" in html
    assert 'href="javascript' not in html and "[bad](javascript:alert(1))" in html
    assert '<a href="https://example.org/a?b=1&amp;c=2">ok</a>' in html
    assert "<code>&lt;b&gt;code&lt;/b&gt;</code>" in html
    assert 'data-open-doc="t-0018"' in html
    assert 'data-open-doc="t-0017"' not in html and 'data-open-doc="t-0099"' not in html


def test_tables_lists_quotes_and_code_render_completely() -> None:
    source = (
        "| left | middle | right |\n| :--- | :---: | ---: |\n| a | **b** | c |\n\n"
        "- one\n  - nested\n- [x] done\n- [ ] open\n\n"
        "> quoted *words*\n\n---\n\n```\n<raw>\n```"
    )
    html = render(source)
    assert '<th style="text-align:left">left</th><th style="text-align:center">middle</th>' in html
    assert '<td style="text-align:right">c</td>' in html and "<b>b</b>" in html
    assert "<li>one<ul><li>nested</li></ul></li>" in html
    assert '<li class="task"><input type="checkbox" disabled checked> done</li>' in html
    assert '<li class="task"><input type="checkbox" disabled> open</li>' in html
    assert "<blockquote><p>quoted <i>words</i></p></blockquote><hr>" in html
    assert "<pre><code>&lt;raw&gt;</code></pre>" in html
