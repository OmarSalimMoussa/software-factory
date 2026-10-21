"""What a tester's session reads of its browsers' answers (wrapper.py).

Each text is cut at ``qa.browser_answer_chars``, with a note to read on: Claude Code refuses a tool's answer past its
own cap, which a snapshot of a page with a recorded page in its frame passes.

A live list draws its rows anew on each poll, so the refs of the snapshot a tester read go stale, and Playwright
refuses a call naming one before it acts. The answers keep, for each browser, the role and name of every element in
the last snapshot the session read. On that refusal the wrapper looks at the page itself: when exactly one element
has that role and name now, it sends the call again with that element's ref, and the answer says so. When none or
several have it, it does not guess: the refusal reaches the tester with how many there are. Only a tool that finds
every element it names before it acts goes again, so no call that may have acted runs twice: browser_fill_form finds
each field after it filled the one before. A snapshot prints an element as its role, its name when it has one, its
states and its ref, the line in single quotes when it holds a colon.

Each answer that names a page says who the factory last signed that browser in as, or that it signed it in as no
one, so a tester with two people at once never has to track which address is in which browser.
"""

from __future__ import annotations

import re
from typing import Any

CUT = (
    "\n\n[The factory cut this answer here: the page holds more than one answer can carry. Read what you need "
    "with browser_evaluate, or look with browser_take_screenshot.]"
)
AGAIN = frozenset({
    "browser_click", "browser_type", "browser_hover", "browser_select_option", "browser_drag", "browser_drop",
    "browser_evaluate", "browser_snapshot", "browser_take_screenshot",
})  # fmt: skip
TARGETS = ("target", "startTarget", "endTarget")
Stale = tuple[dict[str, Any], str, str]
PAGE_URL = re.compile(r"Page URL: (.+)")
STALE = re.compile(r"^Error: Ref ((?:f\d+)?e\d+) not found in the current page snapshot", re.M)
ELEMENT = re.compile(r"""^\s*- '?([\w-]+)( "(?:[^"\\]|\\.)*")?(?: \[[^\]\s]+\])* \[ref=((?:f\d+)?e\d+)\]""", re.M)


def elements(text: str) -> dict[str, str]:
    """Each element's ref in a snapshot, with its role and name as the snapshot prints them."""
    return {ref: role + name for role, name, ref in ELEMENT.findall(text)}


def _texts(result: dict[str, Any]) -> list[dict[str, Any]]:
    content = result.get("content")
    return [c for c in content if isinstance(c, dict) and c.get("type") == "text"] if isinstance(content, list) else []


def text_of(result: dict[str, Any]) -> str:
    return "\n".join(str(c.get("text", "")) for c in _texts(result))


class Answers:
    def __init__(self, most: int) -> None:
        self._most = most
        self._seen: dict[str, dict[str, str]] = {}
        self._asked: dict[str, dict[str, Any]] = {}
        self._notes: dict[str, str] = {}
        self._signed: dict[str, str | None] = {}

    def shown(self, key: Any, which: str, result: dict[str, Any]) -> None:
        texts = _texts(result)
        if texts:
            texts[0]["text"] = self.paged(which, self.told(key, str(texts[0].get("text"))))
        for part in texts:
            if len(str(part.get("text"))) > self._most:
                part["text"] = str(part["text"])[: self._most] + CUT
        self.saw(which, text_of(result))

    def signed_in(self, which: str, address: str | None) -> None:
        self._signed[which] = address

    def paged(self, which: str, text: str) -> str:
        """``text``, when it names a page, with who the factory last signed ``which`` in as."""
        found, who = PAGE_URL.search(text), self._signed.get(which)
        said = "Not signed in by the factory" if who is None else f"Signed in as {who}"
        return text if found is None else f"{text[: found.end()]}\n- {said}{text[found.end() :]}"

    def saw(self, which: str, text: str) -> None:
        found = elements(text)
        if found:
            self._seen[which] = found

    def sent(self, message: dict[str, Any]) -> None:
        if message["params"].get("name") in AGAIN:
            self._asked[str(message.get("id"))] = message

    def asked(self, key: Any) -> dict[str, Any] | None:
        return self._asked.pop(str(key), None)

    def told(self, key: Any, text: str) -> str:
        """The answer to a call the factory sent again, after what it did."""
        self._asked.pop(str(key), None)
        note = self._notes.pop(str(key), None)
        return text if note is None else note + text

    def stale(self, asked: dict[str, Any] | None, which: str, refused: str) -> Stale | None:
        """``asked``, when it may go again, with the ref ``refused`` names as stale and what the session saw it as."""
        found = STALE.search(refused)
        if asked is None or found is None or which not in self._seen:
            return None
        what = self._seen[which].get(found.group(1))
        return None if what is None else (asked, found.group(1), what)

    def again(self, asked: dict[str, Any], which: str, old: str, new: str, what: str) -> dict[str, Any]:
        """``asked`` with ``new`` where it named ``old``; its answer will say so."""
        self._seen[which][new] = what
        self._notes[str(asked.get("id"))] = (
            f"The page drew {what} anew, so ref {old} is {new} now: the factory sent your call again with it.\n\n"
        )
        args = {k: new if k in TARGETS and v == old else v for k, v in asked["params"]["arguments"].items()}
        return {**asked, "params": {**asked["params"], "arguments": args}}
