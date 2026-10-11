"""The queue as the warden reads and writes it: requests, tasks, specs, checks and task logs, through the safe file
layer, with queue-format run on every write.

A file that cannot be read is returned as a broken row naming it, never skipped; a write whose files fail
queue-format is refused whole.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.queue import format_check, formats, records
from runtime.queue.files import QueueFiles
from runtime.queue.formats import FormatError
from runtime.queue.records import CheckRecord, Spec
from runtime.shared.models import INBOXES, Request, Task
from runtime.shared.protocol import Refused
from runtime.warden.context import Context

LOG_SUFFIX = ".log.jsonl"


@dataclass(frozen=True, slots=True)
class Broken:
    file: str
    error: str


def request_rel(req: Request) -> str:
    return f"requests/{req.inbox}/{req.id}.md"


def task_rel(task_id: str) -> str:
    return f"tasks/{task_id}.md"


def log_rel(task_id: str) -> str:
    return f"tasks/{task_id}{LOG_SUFFIX}"


def spec_rel(task_id: str) -> str:
    return f"specs/{task_id}.md"


class Queue:
    def __init__(self, files: QueueFiles) -> None:
        self.files = files

    @classmethod
    def of(cls, ctx: Context) -> Queue:
        """The queue of the repo the settings name; it must be a folder the warden owns."""
        return cls(QueueFiles(Path(ctx.settings().get_str("repo.path")), os.geteuid()))

    @contextmanager
    def lock(self) -> Iterator[None]:
        with self.files.lock():
            yield

    def stamps(self) -> dict[str, tuple[int, int, int]]:
        """Every request, task, task log, spec and check, by its path under queue/, with what says it changed."""
        folders = [f"requests/{inbox}" for inbox in INBOXES] + ["tasks", "specs", "checks"]
        return {f"{folder}/{name}": stamp for folder in folders for name, stamp in self.files.stamps(folder).items()
                if name.endswith((".md", LOG_SUFFIX))}  # fmt: skip

    def blobs(self) -> dict[str, bytes]:
        """Every request, task, task log, spec and check, by its path under queue/. Only a file that changed since the
        warden last read it is read again: the queue only grows, and every write checks all of it."""
        return {rel: self.files.read_stamped(rel, stamp) for rel, stamp in self.stamps().items()}

    def requests(self) -> tuple[list[Request], list[Broken]]:
        good: list[Request] = []
        broken: list[Broken] = []
        for inbox in INBOXES:
            for name in self.files.names(f"requests/{inbox}"):
                if name.endswith(".md"):
                    rel = f"requests/{inbox}/{name}"
                    try:
                        good.append(formats.parse_request(self.files.read(rel).decode(), rel))
                    except (FormatError, UnicodeDecodeError) as exc:
                        broken.append(Broken(rel, str(exc)))
        return good, broken

    def request(self, request_id: str) -> Request:
        if not formats.REQUEST_ID.match(request_id):
            raise Refused("request", f"{request_id!r} is not a request id")
        for inbox in INBOXES:
            rel = f"requests/{inbox}/{request_id}.md"
            try:
                raw = self.files.read(rel)
            except FileNotFoundError:
                continue
            return formats.parse_request(raw.decode(), rel)
        raise FileNotFoundError(request_id)

    def tasks(self) -> tuple[list[Task], list[Broken]]:
        good: list[Task] = []
        broken: list[Broken] = []
        for name in self.files.names("tasks"):
            if name.endswith(".md"):
                rel = f"tasks/{name}"
                try:
                    good.append(formats.parse_task(self.files.read(rel).decode(), rel))
                except (FormatError, UnicodeDecodeError) as exc:
                    broken.append(Broken(rel, str(exc)))
        return good, broken

    def task(self, task_id: str) -> Task:
        """The task; FileNotFoundError when there is none, FormatError when it is malformed."""
        if not formats.TASK_ID.match(task_id):
            raise Refused("task", f"{task_id!r} is not a task id")
        return formats.parse_task(self.files.read(task_rel(task_id)).decode(), task_rel(task_id))

    def spec(self, task_id: str) -> Spec | None:
        try:
            raw = self.files.read(spec_rel(task_id))
        except FileNotFoundError:
            return None
        return records.parse_spec(raw.decode(), spec_rel(task_id))

    def checks(self) -> tuple[list[CheckRecord], list[Broken]]:
        good: list[CheckRecord] = []
        broken: list[Broken] = []
        for name in self.files.names("checks"):
            rel = f"checks/{name}"
            try:
                good.append(records.parse_check(self.files.read(rel).decode(), rel))
            except (FormatError, UnicodeDecodeError) as exc:
                broken.append(Broken(rel, str(exc)))
        return good, broken

    def write(self, changes: Mapping[str, str], pictures: Mapping[str, bytes] | None = None) -> None:
        """Write every file at once, after queue-format passes on each of them in the queue they make.

        ``pictures`` are written in the same step; they are images, so queue-format does not read them.
        """
        encoded = {rel: text.encode() for rel, text in changes.items()}
        projected = {**self.blobs(), **encoded}
        problems = format_check.check(projected)
        bad = [f"{rel}: {'; '.join(problems[rel])}" for rel in encoded if problems.get(rel)]
        if bad:
            raise Refused("queue-format", " | ".join(bad))
        if any(not rel.startswith("pictures/") for rel in pictures or {}):
            raise ValueError("only pictures go beside a queue write")
        self.files.write_many({**encoded, **(pictures or {})})

    def write_task(self, task: Task) -> None:
        self.write({task_rel(task.id): formats.dump_task(task)})

    def log(self, task_id: str, at: str, actor: str, event: str, text: str, **extra: str) -> None:
        """One line in the task's log (R16); text loses its newlines."""
        entry: dict[str, Any] = {"at": at, "actor": actor, "event": event, "text": formats.one_line(text), **extra}
        self.files.append(log_rel(task_id), records.dump_log(entry))

    def read_log(self, task_id: str) -> list[dict[str, str]]:
        try:
            raw = self.files.read(log_rel(task_id))
        except FileNotFoundError:
            return []
        return records.parse_log(raw.decode(), log_rel(task_id))

    def next_request_id(self, day: str) -> str:
        """The next r-YYYY-MM-DD-NNNN for ``day`` (YYYY-MM-DD), across every inbox."""
        prefix = f"r-{day}-"
        used = [
            int(name[len(prefix) : -3])
            for inbox in INBOXES
            for name in self.files.names(f"requests/{inbox}")
            if name.startswith(prefix) and name.endswith(".md") and name[len(prefix) : -3].isdigit()
        ]
        return f"{prefix}{max(used, default=0) + 1:04d}"

    def next_task_id(self) -> str:
        used = [int(n[2:-3]) for n in self.files.names("tasks") if n.endswith(".md") and n[2:-3].isdigit()]
        return f"t-{max(used, default=0) + 1:04d}"
