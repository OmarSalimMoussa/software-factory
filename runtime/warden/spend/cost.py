"""Every model call's cost, read from Claude Code's transcripts and appended to cost.jsonl; in the same pass, where
each agent's time went (partition.py), appended to time.jsonl.

Each pass reads only the lines added since the last one (a byte cursor per transcript, kept in cost-cursor.json),
for every registered session and every subagent under it. Claude Code writes one API call as two or three adjacent
lines that repeat its usage, so a call is counted once per ``message.id`` + ``requestId``. A call is priced with
``[prices]``; a model with no price costs unknown (None), never zero. A failed call is recorded too.

Who pays, in money and in time: a session's calls go to its task; a subagent's to its parent's task, except an intake
run, charged to the request it debugs (rolled into the task the request joins), and triage, which is upkeep (no task).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runtime.queue.formats import iso
from runtime.shared import models, store
from runtime.shared.models import CostRecord, Session, TimeRecord
from runtime.warden.context import Context
from runtime.warden.sessions import registry
from runtime.warden.spend import partition

log = logging.getLogger("factory.warden")

UPKEEP = ""
# Claude Code names a session's transcript folder after its working directory, every other character a dash.
_SLUG = re.compile(r"[^A-Za-z0-9]")
# A reply Claude Code wrote itself for a failed request names this model.
_SYNTHETIC = "<synthetic>"
# A failed call's words when the account is at its plan's limit.
_LIMIT = re.compile(r"usage limit|rate.?limit|\b429\b", re.IGNORECASE)
_FAMILIES = {"intake": "intake", "triage": "intake", "critic": "design", "qa": "qa"}


@dataclass(frozen=True, slots=True)
class Payer:
    session: str
    subagent: str
    role: str
    family: str
    task: str
    model: str  # the model a failed call was meant for, when the transcript names none


def family_of(kind: str, parent_family: str) -> str:
    return _FAMILIES.get(kind, _FAMILIES.get(kind.split("-", 1)[0], parent_family))


def transcript(roots: Sequence[Path], session: Session) -> Path | None:
    """A session's transcript under the first root that holds it."""
    return transcript_of(roots, session.worktree, session.id)


def transcript_of(roots: Sequence[Path], worktree: str, session_id: str) -> Path | None:
    """The transcript of session ``session_id``, which ran in ``worktree`` ("" when unknown)."""
    for root in roots:
        direct = root / _SLUG.sub("-", worktree) / f"{session_id}.jsonl"
        if direct.is_file():
            return direct
        found = next(iter(sorted(root.glob(f"*/{session_id}.jsonl"))), None)
        if found is not None:
            return found
    return None


def _subagent_payer(parent: Session, agent_id: str, meta: Path, rows: dict[str, Session]) -> Payer:
    row = rows.get(agent_id)
    if row is not None:
        kind, family, task, model = row.role, row.family, row.task, row.model
    else:
        try:
            kind = str(json.loads(meta.read_text()).get("agentType") or "")
        # telemetry: expected — a subagent whose record is not written yet is charged to its parent's role
        except (OSError, ValueError):
            kind = ""
        family, task, model = family_of(kind, parent.family), parent.task, ""
        # An unregistered intake run names no request; its cost stays with the parent's own work.
    if kind == "triage":
        task = UPKEEP
    return Payer(parent.id, agent_id, kind or parent.role, family, task, model)


def _price(settings_price: dict[str, float] | None, usage: dict[str, int]) -> float | None:
    if settings_price is None:
        return None
    total = sum(usage[name] * settings_price[name] for name in ("input", "output", "cache_read", "cache_write"))
    return round(total / 1_000_000, 6)


def _tokens(usage: object) -> dict[str, int]:
    """A call's token counts; a usage without one of them is not one Claude Code wrote."""
    if not isinstance(usage, dict):
        raise TypeError("usage is not an object")
    names = {"input": "input_tokens", "output": "output_tokens", "cache_read": "cache_read_input_tokens",
             "cache_write": "cache_creation_input_tokens"}  # fmt: skip
    tokens = {ours: usage[theirs] for ours, theirs in names.items()}
    if not all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in tokens.values()):
        raise TypeError("a token count is not a whole number")
    if not any(tokens.values()) and isinstance(usage.get("iterations"), list):
        # Some calls carry their counts only in their parts, and read zero at the top.
        parts = [_tokens(part) for part in usage["iterations"]]
        tokens = {name: sum(part[name] for part in parts) for name in tokens}
    return tokens


def call_outcome(line: str, since: float) -> str | None:
    """How one transcript line's model call after ``since`` went: ok, limit (the plan's), or the error's first line;
    None for a line that is no such call."""
    try:
        entry = json.loads(line)
        at = datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00")).timestamp()
    # telemetry: expected — a line still being written, or one that is not a call, holds no outcome: `None`
    except (ValueError, TypeError, KeyError):
        return None
    if entry.get("type") != "assistant" or at < since:
        return None
    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    if not entry.get("isApiErrorMessage") and message.get("model") != _SYNTHETIC:
        return "ok"
    text = " ".join(b.get("text", "") for b in message.get("content") or [] if isinstance(b, dict)).strip()
    return "limit" if _LIMIT.search(text) else (text.splitlines()[0][:200] if text else "the call failed")


def _record(entry: dict[str, Any], payer: Payer, model_seen: str, ctx: Context) -> CostRecord:
    message = entry["message"]
    tokens = _tokens(message["usage"])
    model = str(message.get("model") or "")
    failed = bool(entry.get("isApiErrorMessage")) or model == _SYNTHETIC
    if model == _SYNTHETIC or not model:
        model = model_seen or payer.model
    at = datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00")).astimezone(UTC)
    # A call that worked and counted nothing was not counted: its price is unknown, never $0.
    usd = _price(ctx.settings().price(model) if model else None, tokens) if failed or any(tokens.values()) else None
    return CostRecord(at=at.strftime("%Y-%m-%dT%H:%M:%SZ"), session=payer.session, subagent=payer.subagent,
                      role=payer.role, family=payer.family, task=payer.task, model=model, usd=usd, failed=failed,
                      **tokens)  # fmt: skip


def _new_lines(path: Path, offset: int) -> tuple[bytes, int]:
    """The whole lines written after ``offset``, and the offset after them; a line still being written waits."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        size = os.fstat(fd).st_size
        if size < offset:
            # Claude Code only appends; a shorter file was replaced, and reading it again would count calls twice.
            log.warning("warden: a transcript shrank below its cost cursor (%s); its new lines are read from here",
                        path.name)  # fmt: skip
            return b"", size
        os.lseek(fd, offset, os.SEEK_SET)
        chunks, left = [], size - offset
        while left > 0:
            chunk = os.read(fd, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    whole = data[: data.rfind(b"\n") + 1]
    return whole, offset + len(whole)


def read_file(
    ctx: Context, path: Path, payer: Payer, cursor: dict[str, Any], copied: str = ""
) -> tuple[list[CostRecord], TimeRecord | None]:
    """The calls added to one transcript since its cursor, each once, and where the time since went; moves the cursor
    past them. Lines older than ``copied``, when a session's copy was made, repeat its original's, counted then."""
    mark = cursor.setdefault(str(path), {"offset": 0, "last": "", "model": "", "time": partition.fresh(True)})
    data, offset = _new_lines(path, mark["offset"])
    if offset < mark["offset"]:
        mark["time"] = partition.fresh(False)
    before = datetime.fromisoformat(copied.replace("Z", "+00:00")) if copied else None
    records, tally = [], partition.Tally()
    for number, line in enumerate(data.splitlines(), 1):
        try:
            entry = json.loads(line)
        except ValueError:
            log.warning("warden: cost skipped an unreadable transcript line (%s, line %d of a pass)", path.name, number)
            continue
        if not isinstance(entry, dict) or _copied(entry, before):
            continue
        partition.step(mark["time"], entry, tally)
        message = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(message, dict) or "usage" not in message:
            continue
        key = f"{message.get('id')}:{entry.get('requestId')}"
        if key == mark["last"]:
            continue
        mark["last"] = key
        try:
            record = _record(entry, payer, mark["model"], ctx)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("warden: cost skipped a call it cannot read (%s, %s)", path.name, type(exc).__name__)
            continue
        if not record.failed:
            mark["model"] = record.model
        records.append(record)
    mark["offset"] = offset
    return records, _timed(tally, payer, mark["time"]["whole"])


def _timed(tally: partition.Tally, payer: Payer, whole: bool) -> TimeRecord | None:
    if tally.first is None or tally.last is None:
        return None
    return TimeRecord(first=iso(tally.first), last=iso(tally.last), session=payer.session, subagent=payer.subagent,
                      role=payer.role, family=payer.family, task=payer.task, model=round(tally.model, 3),
                      tools={k: round(v, 3) for k, v in tally.tools.items()}, waiting=round(tally.waiting, 3),
                      idle=round(tally.idle, 3), whole=whole)  # fmt: skip


def _copied(entry: dict[str, Any], before: datetime | None) -> bool:
    if before is None:
        return False
    try:
        return datetime.fromisoformat(str(entry["timestamp"]).replace("Z", "+00:00")) < before
    # telemetry: expected — a line with no readable time is left to `_record`, which skips and reports it
    except (KeyError, ValueError):
        return False


def run_pass(ctx: Context, roots: Sequence[Path]) -> int:
    """Read every registered session's and subagent's new calls and time; returns how many calls were recorded."""
    sessions = registry.read(ctx.places)
    cursor = store.read_json_or(ctx.places.cost_cursor, {})
    if not isinstance(cursor, dict):
        raise store.StoreError(f"{ctx.places.cost_cursor}: not an object")
    by_agent = {s.agent_id: s for s in sessions.values() if s.agent_id}
    records: list[CostRecord] = []
    times: list[TimeRecord] = []

    def take(path: Path, payer: Payer, copied: str = "") -> None:
        calls, time = read_file(ctx, path, payer, cursor, copied)
        records.extend(calls)
        times.extend([time] if time is not None else [])

    for session in sessions.values():
        if session.agent_id:
            continue  # read through its parent's transcript folder
        path = transcript(roots, session)
        if path is None:
            continue
        own = Payer(session.id, "", session.role, session.family, session.task, session.model)
        original = transcript_of(roots, session.worktree, session.copied_from) if session.copied_from else None
        if original is not None:
            take(original, own)
        take(path, own, session.renewed)
        folders = [p.with_suffix("") / "subagents" for p in (path, original) if p is not None]
        for sub in sorted(sub for folder in folders for sub in folder.glob("agent-*.jsonl")):
            agent_id = sub.name.removeprefix("agent-").removesuffix(".jsonl")
            take(sub, _subagent_payer(session, agent_id, sub.with_name(f"agent-{agent_id}.meta.json"), by_agent))
    days = spent_days(ctx)
    for record in records:
        store.append_jsonl(ctx.places.cost, models.dump(record))
        known = days.setdefault(record.at[:10], [0.0, 0])
        known[0 if record.usd is not None else 1] += record.usd if record.usd is not None else 1
    for time in times:
        store.append_jsonl(ctx.places.times, models.dump(time))
    store.write_json(_days_path(ctx), days, mode=0o600)
    store.write_json(ctx.places.cost_cursor, cursor, mode=0o600)
    return len(records)


def _days_path(ctx: Context) -> Path:
    return ctx.places.state / "cost-days.json"


def spent_days(ctx: Context) -> dict[str, list[float]]:
    """Each UTC day's known dollars and its calls with an unknown price, as the passes recorded them."""
    found = store.read_json_or(_days_path(ctx), {})
    if not isinstance(found, dict):
        raise store.StoreError(f"{_days_path(ctx)}: not an object")
    return found
