"""A safe Markdown renderer for the Library's reader, on the server.

Every character of the source is escaped before any markup is made, so nothing in a document reaches the page as
markup but what this module writes: headings, paragraphs, lists (nested, and task lists), tables with alignment,
quotes, rules, code, bold and italic, links to http(s) addresses, and the ids of other documents as links the reader
opens in place. A document never links to itself.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Collection

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$")
_TASK = re.compile(r"^\[([ xX])\]\s+(.*)$")
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_CODE = re.compile(r"(`+)(.+?)\1")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])|(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])")
DOC_ID = re.compile(
    r"(?<![\w-])(t-\d{4,}|s-\d{4,}|r-\d{4}-\d{2}-\d{2}-\d{4}|c-[0-9a-z-]{3,40}|sr\d+|GOAL\.md)(?![\w-])"
)


class _Inline:
    """Inline markup for one run of text: code first, then links, bold, italic and document ids."""

    def __init__(self, known: Collection[str], own: str) -> None:
        self.known, self.own = known, own
        self.held: list[str] = []

    def _hold(self, markup: str) -> str:
        self.held.append(markup)
        return f"\x00{len(self.held) - 1}\x00"

    def _ids(self, text: str) -> str:
        def link(m: re.Match[str]) -> str:
            ref = html.unescape(m.group(1))
            if ref == self.own or ref not in self.known:
                return m.group(0)
            return self._hold(f'<span class="doclink" data-open-doc="{m.group(1)}" tabindex="0">{m.group(1)}</span>')

        return DOC_ID.sub(link, text)

    def _styled(self, text: str) -> str:
        text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
        return _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)

    def render(self, source: str) -> str:
        parts = _CODE.split(source)
        out = []
        # split() with two groups yields: text, fence, code, text, fence, code, ...
        for i in range(0, len(parts), 3):
            text = html.escape(parts[i], quote=True)
            text = _LINK.sub(
                lambda m: self._hold(f'<a href="{m.group(2)}">{self._styled(self._ids(m.group(1)))}</a>'), text
            )
            out.append(self._styled(self._ids(text)))
            if i + 2 < len(parts):
                out.append(self._hold(f"<code>{html.escape(parts[i + 2].strip(), quote=True)}</code>"))
        joined = "".join(out)
        while "\x00" in joined:
            joined = re.sub(r"\x00(\d+)\x00", lambda m: self.held[int(m.group(1))], joined)
        return joined


def _cells(line: str) -> list[str]:
    row = line.strip()
    row = row[1:] if row.startswith("|") else row
    row = row[:-1] if row.endswith("|") and not row.endswith("\\|") else row
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


def _align(cell: str) -> str:
    left, right = cell.startswith(":"), cell.endswith(":")
    side = "center" if left and right else "right" if right else "left" if left else ""
    return f' style="text-align:{side}"' if side else ""


def _table(lines: list[str], inline: Callable[[str], str]) -> str:
    head, aligns = _cells(lines[0]), [_align(c) for c in _cells(lines[1])]
    width = len(head)
    aligns = (aligns + [""] * width)[:width]
    out = ['<div class="table-wrap"><table><thead><tr>']
    out += [f"<th{aligns[i]}>{inline(c)}</th>" for i, c in enumerate(head)]
    out.append("</tr></thead><tbody>")
    for line in lines[2:]:
        cells = (_cells(line) + [""] * width)[:width]
        out.append("<tr>" + "".join(f"<td{aligns[i]}>{inline(c)}</td>" for i, c in enumerate(cells)) + "</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def _list(items: list[tuple[int, bool, str]], inline: Callable[[str], str]) -> str:
    """Items as (indent, ordered, text); a deeper item nests inside the item before it."""
    out: list[str] = []
    i = 0
    indent, ordered = items[0][0], items[0][1]
    tag = "ol" if ordered else "ul"
    out.append(f"<{tag}>")
    while i < len(items):
        _, _, text = items[i]
        task = _TASK.match(text)
        if task:
            box = '<input type="checkbox" disabled' + (" checked" if task.group(1) != " " else "") + ">"
            out.append(f'<li class="task">{box} {inline(task.group(2))}')
        else:
            out.append(f"<li>{inline(text)}")
        j = i + 1
        while j < len(items) and items[j][0] > indent:
            j += 1
        if j > i + 1:
            out.append(_list(items[i + 1 : j], inline))
        out.append("</li>")
        i = j
    out.append(f"</{tag}>")
    return "".join(out)


def _starts_block(line: str, nxt: str) -> bool:
    return bool(
        _HEADING.match(line)
        or _RULE.match(line)
        or _FENCE.match(line)
        or line.lstrip().startswith(">")
        or _ITEM.match(line)
        or ("|" in line and _SEPARATOR.match(nxt))
    )


def _blocks(lines: list[str], inline: Callable[[str], str]) -> str:
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if not line.strip():
            i += 1
            continue
        if fence := _FENCE.match(line):
            block, i = _fenced(lines, i, fence.group(1))
        elif heading := _HEADING.match(line):
            level = min(len(heading.group(1)), 4)
            block, i = f"<h{level}>{inline(heading.group(2))}</h{level}>", i + 1
        elif _RULE.match(line):
            block, i = "<hr>", i + 1
        elif line.lstrip().startswith(">"):
            block, i = _quote(lines, i, inline)
        elif "|" in line and _SEPARATOR.match(nxt):
            end = i + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            block, i = _table(lines[i:end], inline), end
        elif _ITEM.match(line):
            block, i = _items(lines, i, inline)
        else:
            block, i = _paragraph(lines, i, inline)
        out.append(block)
    return "".join(out)


def _fenced(lines: list[str], i: int, fence: str) -> tuple[str, int]:
    end = next((j for j in range(i + 1, len(lines)) if lines[j].strip().startswith(fence)), len(lines))
    code = "\n".join(lines[i + 1 : end])
    return f"<pre><code>{html.escape(code, quote=True)}</code></pre>", end + 1


def _quote(lines: list[str], i: int, inline: Callable[[str], str]) -> tuple[str, int]:
    end = i
    while end < len(lines) and lines[end].lstrip().startswith(">"):
        end += 1
    quoted = [re.sub(r"^\s*>\s?", "", x) for x in lines[i:end]]
    return f"<blockquote>{_blocks(quoted, inline)}</blockquote>", end


def _items(lines: list[str], i: int, inline: Callable[[str], str]) -> tuple[str, int]:
    items: list[tuple[int, bool, str]] = []
    while i < len(lines) and (item := _ITEM.match(lines[i])):
        items.append((len(item.group(1).expandtabs(4)), item.group(2)[0].isdigit(), item.group(3)))
        i += 1
        while i < len(lines) and lines[i].strip() and not _ITEM.match(lines[i]) and lines[i].startswith(" "):
            items[-1] = (items[-1][0], items[-1][1], items[-1][2] + " " + lines[i].strip())
            i += 1
    return _list(items, inline), i


def _paragraph(lines: list[str], i: int, inline: Callable[[str], str]) -> tuple[str, int]:
    end = i + 1
    while (
        end < len(lines)
        and lines[end].strip()
        and not _starts_block(lines[end], lines[end + 1] if end + 1 < len(lines) else "")
    ):
        end += 1
    return f"<p>{inline(' '.join(x.strip() for x in lines[i:end]))}</p>", end


def render(source: str, known: Collection[str] = (), own: str = "") -> str:
    """``source`` as safe HTML; ``known`` ids become links, except ``own``."""
    text = _CONTROL.sub("", source.replace("\r\n", "\n").replace("\r", "\n"))

    def inline(run: str) -> str:
        return _Inline(known, own).render(run)

    return _blocks(text.split("\n"), inline)
