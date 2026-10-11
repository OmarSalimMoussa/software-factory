"""queue-format: the deterministic check of every queue file, alone and against each other (intake spec).

``check`` reads files as bytes by their path under queue/ and returns every problem by file. Every queue write runs
it on the queue the write would make; the warden's sweep runs it over the whole queue every pass and files one
request per failing file and content (R30), so an unchanged broken file is reported once.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from runtime.queue import formats, records
from runtime.queue.formats import FormatError
from runtime.queue.records import CheckRecord, Spec
from runtime.shared import store
from runtime.shared.models import INBOXES, Request, Task

if TYPE_CHECKING:
    from runtime.queue.book import Queue
    from runtime.warden.context import Context


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


def check_queue(repo: Path) -> list[str]:
    """Every problem in a checkout's queue/, as ``queue/<file>: <problem>``; a checkout without one has none.

    This is the standard check's read, as the caller: the warden's own reads go through the safe file layer.
    """
    root = repo / "queue"
    folders = [f"requests/{inbox}" for inbox in INBOXES] + ["tasks", "specs", "checks"]
    blobs = {
        f"{folder}/{path.name}": path.read_bytes()
        for folder in folders
        for path in sorted((root / folder).glob("*"))
        if path.name.endswith((".md", ".log.jsonl")) and path.is_file() and not path.is_symlink()
    }
    return [f"queue/{rel}: {problem}" for rel, found in sorted(check(blobs).items()) for problem in found]


# The queue's stamps at the last sweep, by the warden's state folder.
_SWEPT: dict[str, str] = {}


def _stamp(queue: Queue) -> str:
    return hashlib.sha256(repr(sorted(queue.stamps().items())).encode()).hexdigest()


def sweep(ctx: Context) -> list[str]:
    """Check the whole queue and file one request for each failing file not filed at this content; the new ids."""
    from runtime.queue import intake
    from runtime.queue.book import Queue

    queue = Queue.of(ctx)
    filed_path = ctx.places.state / "queue-format-filed.json"
    with ctx.lock, queue.lock():
        stamp = _stamp(queue)
        if _SWEPT.get(str(ctx.places.state)) == stamp:
            return []  # nothing in the queue changed since the last sweep
        blobs = queue.blobs()
        failing = {rel: found for rel, found in check(blobs).items() if found}
        filed = store.read_json_or(filed_path, {})
        if not isinstance(filed, dict):
            raise store.StoreError(f"{filed_path}: not an object")
        new: list[str] = []
        for rel, found in sorted(failing.items()):
            key = f"{rel}:{hashlib.sha256(blobs[rel]).hexdigest()}"
            if key in filed:
                continue
            body = f"queue-format refuses queue/{rel}:\n\n" + "\n".join(f"- {p}" for p in found)
            req = intake.file_request(ctx, queue, "agents", "warden", "normal", f"queue/{rel}", body)
            filed[key] = req.id
            new.append(req.id)
        if new:
            store.write_json(filed_path, filed, mode=0o600)
            stamp = _stamp(queue)  # with what it filed
        _SWEPT[str(ctx.places.state)] = stamp
    return new
