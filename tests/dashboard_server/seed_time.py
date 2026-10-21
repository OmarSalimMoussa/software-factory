"""The afternoon's time as the warden keeps it: the done tasks' moves from proposed to on staging in their logs, and
what their agents' time went to. One of them has no runtime measured, so it reads unknown."""

from __future__ import annotations

from pathlib import Path

from seed_kit import iso

from runtime.queue import records
from runtime.shared import models, store
from runtime.shared.models import TimeRecord
from runtime.shared.paths import Places

EVENTS = {
    "proposed": "propose",
    "approved": "approve",
    "designing": "start",
    "building": "start",
    "qa": "qa-ready",
    "qa-clean": "qa-clean",
    "turn": "turn",
    "merged": "merged",
    "checked": "checked",
    "on-staging": "on-staging",
}
# Hours before now that each task reached each status.
_MERGE = (("qa-clean", 7.5), ("turn", 7.4), ("merged", 7), ("checked", 3), ("on-staging", 2.9))
JOURNEYS = {
    "t-0013": (("proposed", 10), ("approved", 9.5), ("building", 9.4), ("qa", 8), *_MERGE),
    "t-0012": (("proposed", 26), ("approved", 25), ("building", 24.8), ("qa", 21), ("qa-clean", 20), ("turn", 19.9),
               ("merged", 5), ("checked", 0.5), ("on-staging", 0.4)),
    "t-0016": (("proposed", 26), ("approved", 25.5), ("building", 25.3), ("qa", 24), ("qa-clean", 23.5),
               ("turn", 23.4), ("merged", 23), ("checked", 12), ("on-staging", 11.9)),
    "t-0015": (("proposed", 20), ("approved", 18), ("designing", 17.9), ("building", 16), ("qa", 14), ("qa-clean", 13),
               ("turn", 12.9), ("merged", 12.5), ("checked", 12), ("on-staging", 11.9)),
}  # fmt: skip
# Each task's lead and one tester: model, tools and idle minutes, and the lead's minutes waiting on its testers.
RUNS = {"t-0013": (18, 25, 40, 6), "t-0012": (35, 60, 95, 12), "t-0016": (22, 30, 20, 0), "t-0017": (12, 20, 8, 4)}
TOOLS = ("Bash factory check", "browser_take_screenshot", "Bash factory slot up", "Read", "Edit")


def seed_time(repo: Path, places: Places, now: float) -> None:
    for task, moves in JOURNEYS.items():
        before = "queued"
        with (repo / "queue" / "tasks" / f"{task}.log.jsonl").open("a") as log:
            for status, hours in moves:
                line = {"at": iso(now - hours * 3600), "actor": "warden", "event": EVENTS[status],
                        "text": f"moved to {status}", "from": before, "to": status}  # fmt: skip
                log.write(records.dump_log(line).decode())
                before = status
    for n, (task, (model, tools, idle, waiting)) in enumerate(RUNS.items()):
        split = {key: tools * 60 / (k + 2) for k, key in enumerate(TOOLS[n % 2 :])}
        for session, subagent, role, family in ((f"impl-{task}", "", "implementer", "impl"),
                                                (f"impl-{task}", f"qa-{task}", "qa-ui-function", "qa")):  # fmt: skip
            share, lead = (0.5, 0.0) if subagent else (1, 60.0)
            record = TimeRecord(iso(now - 3 * 3600), iso(now - 600), session, subagent, role, family, task,
                                model * 60 * share, {k: v * share for k, v in split.items()},
                                waiting * lead, idle * lead, True)  # fmt: skip
            store.append_jsonl(places.times, models.dump(record))
