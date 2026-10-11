"""queue-format: the deterministic check of every queue file, alone and against each other (intake spec).

``check`` reads files as bytes by their path under queue/ and returns every problem by file. Every queue write runs
it on the queue the write would make; the warden's sweep runs it over the whole queue when it changed, and reports
each failing file once at its content as a fault of the warden's own: no agent may write the queue.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from runtime.queue import formats, records
from runtime.queue.formats import FormatError
from runtime.queue.records import CheckRecord, Spec
from runtime.shared import store
from runtime.shared.models import Request, Task

if TYPE_CHECKING:
    from runtime.queue.book import Queue
    from runtime.warden.context import Context


log = logging.getLogger(__name__)

PARSERS: dict[str, Callable[[str, str], object]] = {
    "requests": formats.parse_request,
    "tasks": formats.parse_task,
    "specs": records.parse_spec,
    "checks": records.parse_check,
}


def _parse(rel: str, raw: bytes) -> tuple[object | None, list[str]]:
    try:
        text = raw.decode()
    except UnicodeDecodeError:
        return None, ["the file is not UTF-8 text"]
    folder = rel.split("/", 1)[0]
    try:
        if rel.endswith(".log.jsonl"):
            return records.parse_log(text), []
        parser = PARSERS.get(folder)
        if parser is None:
            return None, [f"{folder}/ holds no queue files"]
        return parser(text, ""), []
    except FormatError as exc:
        return None, exc.problems


def check(blobs: Mapping[str, bytes]) -> dict[str, list[str]]:
    """Every problem, by file; a file with none maps to an empty list."""
    problems: dict[str, list[str]] = {}
    requests: dict[str, tuple[str, Request]] = {}
    tasks: dict[str, tuple[str, Task]] = {}
    specs: list[tuple[str, Spec]] = []
    for rel, raw in blobs.items():
        parsed, problems[rel] = _parse(rel, raw)
        stem = rel.rsplit("/", 1)[-1].removesuffix(".md")
        if isinstance(parsed, Request):
            if rel.split("/")[:2] != ["requests", parsed.inbox] or stem != parsed.id:
                problems[rel].append(f"a request {parsed.id} from {parsed.inbox} lives in requests/{parsed.inbox}/")
            if parsed.id in requests:
                problems[rel].append(f"{parsed.id} is also {requests[parsed.id][0]}")
            requests[parsed.id] = (rel, parsed)
        elif isinstance(parsed, Task):
            if stem != parsed.id:
                problems[rel].append(f"task {parsed.id} lives in tasks/{parsed.id}.md")
            tasks[parsed.id] = (rel, parsed)
        elif isinstance(parsed, Spec):
            if stem != parsed.task:
                problems[rel].append(f"the spec of {parsed.task} lives in specs/{parsed.task}.md")
            specs.append((rel, parsed))
        elif isinstance(parsed, CheckRecord) and stem != parsed.id:
            problems[rel].append(f"check {parsed.id} lives in checks/{parsed.id}.md")
    for task_id, (rel, task) in tasks.items():
        for rid in task.requests:
            if rid not in requests:
                problems[rel].append(f"request {rid} does not exist")
            elif formats.outcome(requests[rid][1].outcome).task != task_id:
                problems[rel].append(f"request {rid}'s outcome does not name {task_id}")
        problems[rel] += [f"after {a} does not exist" for a in task.after if a not in tasks]
    for rid, (rel, req) in requests.items():
        named = formats.outcome(req.outcome).task
        if named and (named not in tasks or rid not in tasks[named][1].requests):
            problems[rel].append(f"its outcome names {named}, which does not link it")
    for rel, spec in specs:
        if spec.task not in tasks:
            problems[rel].append(f"task {spec.task} does not exist")
    return problems


# The queue's stamps at the last sweep, by the warden's state folder.
_SWEPT: dict[str, str] = {}


def _stamp(queue: Queue) -> str:
    return hashlib.sha256(repr(sorted(queue.stamps().items())).encode()).hexdigest()


def sweep(ctx: Context) -> list[str]:
    """Check the whole queue and report each failing file once at its content, as the platform failure it is: only
    the warden writes the queue, so no agent could act on a request about it. The files newly reported."""
    from runtime.queue.book import Queue

    queue = Queue.of(ctx)
    seen_path = ctx.places.state / "queue-format-reported.json"
    with ctx.lock, queue.lock():
        stamp = _stamp(queue)
        if _SWEPT.get(str(ctx.places.state)) == stamp:
            return []  # nothing in the queue changed since the last sweep
        blobs = queue.blobs()
        failing = {rel: found for rel, found in check(blobs).items() if found}
        reported = store.read_json_or(seen_path, [])
        if not isinstance(reported, list):
            raise store.StoreError(f"{seen_path}: not a list")
        keys = {rel: f"{rel}:{hashlib.sha256(blobs[rel]).hexdigest()}" for rel in failing}
        new = sorted(rel for rel, key in keys.items() if key not in reported)
        for rel in new:
            # The problems quote the file, which holds what users wrote: the log names the file and the count only.
            log.error("warden: queue/%s does not fit the queue format (%d problems); the warden alone writes the "
                      "queue, so this is a fault of its own", rel, len(failing[rel]))  # fmt: skip
        store.write_json(seen_path, sorted(keys.values()), mode=0o600)
        _SWEPT[str(ctx.places.state)] = stamp
    return new
