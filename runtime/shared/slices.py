"""A slice's commit names its place in the task with one line of its message: `Slice: 2/4`.

The line counts anywhere in the message, not only in git's trailer block: an agent that adds `Co-Authored-By` in a
paragraph of its own pushes `Slice:` out of the last paragraph, where git alone looks.
"""

from __future__ import annotations

import re

_LINE = re.compile(r"Slice: (\d+)/(\d+)")


class SliceError(ValueError):
    """A message that names two different places."""


def mark(message: str) -> tuple[int, int] | None:
    """The (n, N) a message's `Slice: n/N` line names, or None when it has none."""
    found = {(int(m[1]), int(m[2])) for line in message.splitlines() if (m := _LINE.fullmatch(line.strip()))}
    if len(found) > 1:
        named = ", ".join(f"{n}/{total}" for n, total in sorted(found))
        raise SliceError(f"it has {len(found)} different Slice lines: {named}")
    return next(iter(found), None)
