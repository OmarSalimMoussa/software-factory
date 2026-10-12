"""One run's replay, as the dashboard asks the warden's snapshot for it: the run named by its task, head, tester
and run, read from the evidence without following any link."""

from __future__ import annotations

import json
import re
from typing import Any

from runtime.qa import evidence
from runtime.qa.evidence import TESTER
from runtime.qa.qa_browser import RUN
from runtime.qa.replay.build import DATA, FOLDER
from runtime.queue.formats import TASK_ID
from runtime.shared import protocol
from runtime.shared.protocol import Refused
from runtime.warden.context import Context

HEAD = re.compile(r"^[0-9a-f]{12}$")
# The answer is one JSON line; the replay's data is a small part of it.
_LIMIT = protocol.MAX_LINE // 2


def _names(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    return str(entry.get("task")), str(entry.get("head", ""))[:12], str(entry.get("tester")), str(entry.get("run"))


def read(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """{"replay": {"task", "head", "tester", "run"}} → the run's evidence entry, and its replay: built, failed with
    why, or not built yet (None)."""
    asked = args.get("replay")
    if not isinstance(asked, dict):
        raise Refused("replay", "name the run: its task, head, tester and run")
    task, head, tester, run = (str(asked.get(k, "")) for k in ("task", "head", "tester", "run"))
    if not (TASK_ID.match(task) and HEAD.match(head) and TESTER.match(tester) and RUN.match(run)):
        raise Refused("replay", "a run is named by its task, the head's first 12 characters, its tester and run")
    named = (task, head, tester, run)
    entry = next((e for e in evidence.entries(ctx.places) if _names(e) == named), None)
    if entry is None:
        raise Refused("replay", f"{task} has no run {run} by {tester}; it may have aged out")
    # The nightly prune deletes a run's files and leaves its line in the index.
    if not (ctx.places.evidence / task / head / tester / run).is_dir():
        raise Refused("replay", f"{tester}'s run {run} on {task} has aged out of the evidence")
    try:
        raw = evidence.read_file(ctx.places, [task, head, tester, run, FOLDER, DATA], _LIMIT)
    # telemetry: expected — the warden's replays pass has not reached this run yet; the page says so
    except FileNotFoundError:
        return {"entry": entry, "replay": None}
    return {"entry": entry, "replay": json.loads(raw)}
