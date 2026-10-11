"""A tester's findings, one record each, and the orchestrator's waiver of one the implementer disputes.

A tester records each finding as it finds it (`factory qa finding`), on the head it tests: its severity, where, and
its own words. Its verdict (`factory qa result`) is counted from those records, never typed. qa-clean counts the
open blockers and majors each required tester found on the final head; one the orchestrator waived (`factory qa
waive`, with why) no longer counts. Each task keeps its findings and waivers in the warden's state.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from runtime.queue import formats, intake
from runtime.shared import store
from runtime.shared.protocol import Refused
from runtime.warden.context import Context

SEVERITIES = ("blocker", "major", "minor")
OPEN = ("blocker", "major")
_ID = re.compile(r"^f-\d{1,6}$")


def _path(ctx: Context, task_id: str) -> Path:
    return ctx.places.state / "qa-findings" / f"{task_id}.jsonl"


def _records(ctx: Context, task_id: str) -> list[dict[str, Any]]:
    return store.read_jsonl(_path(ctx, task_id))


def add(
    ctx: Context, task_id: str, head: str, tester: str, agent: str, args: dict[str, Any], data: bytes | None
) -> str:
    """Record one finding of ``tester`` on ``head``; its id."""
    severity, where = args.get("severity"), args.get("where")
    if severity not in SEVERITIES:
        raise Refused("qa-finding", f"--severity is one of {', '.join(SEVERITIES)}")
    if not isinstance(where, str) or not where.strip():
        raise Refused("qa-finding", "--where names the screen, endpoint or step it is on")
    text = intake.body_text(data, "finding", ctx.settings().get_int("queue.max_upload_kib"))
    with ctx.lock:
        found = sum(1 for r in _records(ctx, task_id) if "finding" in r)
        finding = f"f-{found + 1}"
        store.append_jsonl(_path(ctx, task_id), {"finding": finding, "head": head, "tester": tester, "agent": agent,
                                                  "severity": severity, "where": formats.one_line(where),
                                                  "text": text.strip(), "at": ctx.clock.iso()})  # fmt: skip
    return finding


def waive(ctx: Context, task_id: str, finding: str, why: str) -> None:
    """The orchestrator rules a disputed finding no longer counts, with why."""
    if not _ID.match(finding) or not any(r.get("finding") == finding for r in _records(ctx, task_id)):
        raise Refused("qa-waive", f"{task_id} has no finding {finding}")
    if not why.strip():
        raise Refused("qa-waive", "say why the finding no longer counts: --why '…'")
    store.append_jsonl(_path(ctx, task_id), {"waived": finding, "why": formats.one_line(why), "at": ctx.clock.iso()})


def counts(ctx: Context, task_id: str, head: str, **who: str) -> dict[str, int]:
    """Each severity's findings on ``head`` that were not waived, from the tester or the agent ``who`` names."""
    records = _records(ctx, task_id)
    waived = {r["waived"] for r in records if "waived" in r}
    found = {s: 0 for s in SEVERITIES}
    for r in records:
        if (
            "finding" in r
            and r["head"] == head
            and r["finding"] not in waived
            and all(r[k] == v for k, v in who.items())
        ):
            found[r["severity"]] += 1
    return found


def open_count(ctx: Context, task_id: str, head: str, tester: str) -> int:
    found = counts(ctx, task_id, head, tester=tester)
    return sum(found[s] for s in OPEN)
