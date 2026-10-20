"""Whether a browser tester walked every width (`qa.widths`), read at its hand-back from what its browser recorded,
never from what the tester says: each frame of a run's trace names the width of the page it showed.

The runs still sit in the agent's folder, so no link is followed and only plain files are read, as the evidence's
copy does.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from runtime.qa import evidence
from runtime.qa.replay import trace
from runtime.shared.paths import Places


def unwalked(places: Places, runs: Path, session_id: str, started_as: str, since: str, widths: list[int]) -> list[int]:
    """Each of ``widths`` that no frame of the tester's runs since ``since`` showed, in the order given."""
    shown: set[int] = set()
    for run in evidence.walks(places, runs, session_id, started_as, since):
        shown |= _shown(run / "traces")
    return [width for width in widths if width not in shown]


def _shown(traces: Path) -> set[int]:
    if traces.is_symlink() or not traces.is_dir():
        return set()
    shown: set[int] = set()
    for path in sorted(traces.glob("*.trace")):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                continue
            raise
        with os.fdopen(fd, encoding="utf-8", errors="replace") as lines:
            if stat.S_ISREG(os.fstat(lines.fileno()).st_mode):
                shown |= trace.widths(lines)
    return shown
