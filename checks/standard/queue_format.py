"""Queue format: every request, task and spec in the product's queue/ fits the formats the intake spec sets.

The queue is ignored by git, so this reads the working folder, not the tracked files. The formats live with the
queue commands (runtime/queue/formats.py), which run the same check on every write; this check is that one function.
"""

from __future__ import annotations

from typing import Any

from checks.runner.repo import Repo

ID = "queue-format"
RULE = "Every request, task and spec in queue/ has its format"
VALUES_FILE = None
VALUES = None


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    # Imported here so a fault in the queue's code fails this check alone, not the catalog every check reads.
    from runtime.queue.format_check import check_queue

    return check_queue(repo.root)
