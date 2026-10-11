"""Documents sessions save into the queue through the warden: a designer's draft spec, and the orchestrator's
saved check of the default branch. Each arrives as bytes the caller read itself, and must parse before it is kept.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from runtime.queue import formats, intake, records
from runtime.queue.book import Queue, spec_rel, task_rel
from runtime.queue.formats import FormatError
from runtime.queue.records import CheckRecord, Spec
from runtime.shared import store
from runtime.shared.protocol import Refused
from runtime.warden.context import Context

DESIGNED = ("designing", "review")


def _parsed(text: str, what: str) -> tuple[str, str]:
    head, _, body = text.strip().partition("\n")
    if not head.startswith("# ") or not body.strip():
        raise Refused(what, f"the {what} starts with its # title and has text under it")
    return head[2:].strip(), body.strip()


def _slices_path(ctx: Context) -> Path:
    return ctx.places.state / "spec-slices.json"


def save_spec(ctx: Context, task_id: str, data: bytes | None, slices: object) -> Spec:
    """A designer's draft spec of its task, with the number of slices it lists; a revision keeps the spec's status
    and brief. The count becomes the task's when the spec is approved: it routes the build and qa-ready holds it."""
    if isinstance(slices, bool) or not isinstance(slices, int) or slices < 1:
        raise Refused("slices", "name the number of slices the spec lists: --slices <N>")
    text = intake.body_text(data, "spec", ctx.settings().get_int("queue.max_upload_kib"))
    title, body = _parsed(text, "spec")
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = queue.task(task_id)
        if task.status not in DESIGNED:
            raise Refused("spec", f"{task_id} is {task.status}; a spec is saved while it is designed")
        old = queue.spec(task_id)
        spec = Spec(task_id, task.goal_item, task.run, "draft", "", ctx.clock.iso(), title, body)
        if old is not None:
            spec = dataclasses.replace(spec, status=old.status, brief=old.brief)
        queue.write({spec_rel(task_id): records.dump_spec(spec)})
        queue.log(task_id, ctx.clock.iso(), "designer", "spec", f"spec saved: {title}, {slices} slices")
        counts = store.read_json_or(_slices_path(ctx), {})
        store.write_json(_slices_path(ctx), {**counts, task_id: slices}, mode=0o600)
    return spec


def save_check(ctx: Context, commit: str, data: bytes | None) -> CheckRecord:
    """A check of the default branch: the report's # title, its ## Scenarios table and any notes."""
    if not formats.SHA.match(commit):
        raise Refused("check", "name the commit the check ran on")
    text = intake.body_text(data, "check", ctx.settings().get_int("queue.max_upload_kib"))
    now = ctx.clock.iso()
    record_id = f"c-{now.replace('-', '').replace(':', '')}-{commit[:7]}"
    full = f"---\nid: {record_id}\ncommit: {commit}\nat: {now}\n---\n{text.strip()}\n"
    try:
        record = records.parse_check(full, f"checks/{record_id}.md")
    except FormatError as exc:
        raise Refused("check", "; ".join(exc.problems)) from exc
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        queue.write({f"checks/{record_id}.md": records.dump_check(record)})
    return record


def approve_spec(ctx: Context, queue: Queue, task_id: str) -> None:
    """The spec of a task whose build may start is approved, and the task takes the spec's slice count; the caller
    holds the queue lock."""
    spec = queue.spec(task_id)
    if spec is not None:
        approved = dataclasses.replace(spec, status="approved", updated=ctx.clock.iso())
        queue.write({spec_rel(task_id): records.dump_spec(approved)})
    count = store.read_json_or(_slices_path(ctx), {}).get(task_id)
    task = queue.task(task_id)
    if isinstance(count, int) and count != task.slices:
        queue.write({task_rel(task_id): formats.dump_task(dataclasses.replace(task, slices=count))})
        queue.log(task_id, ctx.clock.iso(), "warden", "facts", f"slices {count}, as the approved spec lists")
