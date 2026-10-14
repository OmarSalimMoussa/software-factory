"""Designers' briefs, kept on this machine with their tasks: every saved version of the page, and the owner's
comments on them. The designer saves, reads and answers through the factory command; the owner reads and comments on
the dashboard. Nothing here depends on the Claude account the agents run under.

A brief is one self-contained HTML page, its pictures inline. A comment keeps the text the owner selected with a
little of what came before and after it, so a later version finds it again; one whose text is gone shows the version
it was made on. A thread is open until the designer answers it, and answered until the owner resolves it.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from runtime.queue.formats import TASK_ID
from runtime.shared import store
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused, ok
from runtime.warden.context import Context
from runtime.warden.sessions import registry

OPEN, ANSWERED, RESOLVED = "open", "answered", "resolved"


def _dir(places: Places, task: str) -> Path:
    return places.state / "briefs" / task


def read(places: Places, task: str) -> dict[str, Any]:
    """A task's brief: its versions, each {n, at}, oldest first, and its comment threads."""
    found: dict[str, Any] = store.read_json_or(_dir(places, task) / "brief.json", {"versions": [], "threads": []})
    return found


def _write(places: Places, task: str, brief: dict[str, Any]) -> None:
    store.write_json(_dir(places, task) / "brief.json", brief, mode=0o600)


def _task(args: dict[str, Any]) -> str:
    task = args.get("task")
    if not isinstance(task, str) or not TASK_ID.match(task):
        raise Refused("brief", "name the task the brief belongs to, such as t-0044")
    return task


def _text(args: dict[str, Any], key: str, limit: int, required: bool = True) -> str:
    value = args.get(key, "")
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise Refused("brief", f"{key} is text of at most {limit} characters" + (", and not empty" if required else ""))
    return value


def _designer_of(ctx: Context, task: str) -> None:
    session = registry.read(ctx.places).get(ctx.caller.session)
    if session is None or session.task != task:
        raise Refused("brief", f"only the designer of {task} saves or answers its brief")


def _version(brief: dict[str, Any], raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= len(brief["versions"]):
        raise Refused("brief", f"name a saved version, from 1 to {len(brief['versions'])}")
    return raw


def _thread(brief: dict[str, Any], raw: object) -> dict[str, Any]:
    found: dict[str, Any] | None = next((t for t in brief["threads"] if t["id"] == raw), None)
    if found is None:
        raise Refused("brief", f"there is no comment {raw!r} on this brief")
    return found


def save(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The designer saves a new version of its task's brief."""
    task = _task(args)
    _designer_of(ctx, task)
    limit = ctx.settings().get_int("queue.max_brief_kib") * 1024
    if not data or not data.strip():
        raise Refused("brief", "name the brief's HTML file: factory brief save <task> <file>")
    if len(data) > limit:
        raise Refused("brief", f"a brief is at most {limit // 1024} KiB, its pictures included")
    try:
        page = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Refused("brief", "the brief is not UTF-8 text") from exc
    with ctx.lock:
        brief = read(ctx.places, task)
        n = len(brief["versions"]) + 1
        store.write_text(_dir(ctx.places, task) / f"v{n}.html", page, mode=0o600)
        brief["versions"].append({"n": n, "at": ctx.clock.iso()})
        _write(ctx.places, task, brief)
    return ok(task=task, version=n)


def page(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """One version's page, for the dashboard to show."""
    task = _task(args)
    n = _version(read(ctx.places, task), args.get("version"))
    raw = (_dir(ctx.places, task) / f"v{n}.html").read_bytes()
    return ok(type="text/html", bytes_b64=base64.b64encode(raw).decode())


def comment(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The owner comments on the text they selected in one version."""
    task, limit = _task(args), ctx.settings().get_int("queue.max_upload_kib") * 1024
    with ctx.lock:
        brief = read(ctx.places, task)
        thread = {
            "id": f"c{len(brief['threads']) + 1}",
            "version": _version(brief, args.get("version")),
            "quote": _text(args, "quote", limit),
            "prefix": _text(args, "prefix", limit, required=False),
            "suffix": _text(args, "suffix", limit, required=False),
            "note": _text(args, "note", limit),
            "at": ctx.clock.iso(),
            "state": OPEN,
            "replies": [],
        }
        brief["threads"].append(thread)
        _write(ctx.places, task, brief)
    return ok(task=task, thread=thread["id"])


def resolve(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The owner closes a thread."""
    task = _task(args)
    with ctx.lock:
        brief = read(ctx.places, task)
        _thread(brief, args.get("thread"))["state"] = RESOLVED
        _write(ctx.places, task, brief)
    return ok(task=task, thread=args.get("thread"))


def comments(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The designer reads every thread the owner has not resolved, with the text each one quotes."""
    task = _task(args)
    _designer_of(ctx, task)
    brief = read(ctx.places, task)
    keep = ("id", "version", "quote", "note", "state", "replies")
    open_ = [{k: t[k] for k in keep} for t in brief["threads"] if t["state"] != RESOLVED]
    return ok(task=task, latest=len(brief["versions"]), threads=open_)


def reply(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The designer answers a thread."""
    task = _task(args)
    _designer_of(ctx, task)
    text = _text(args, "text", ctx.settings().get_int("queue.max_upload_kib") * 1024)
    with ctx.lock:
        brief = read(ctx.places, task)
        thread = _thread(brief, args.get("thread"))
        thread["replies"].append({"by": "designer", "text": text, "at": ctx.clock.iso()})
        thread["state"] = ANSWERED
        _write(ctx.places, task, brief)
    return ok(task=task, thread=thread["id"])


def open_threads(places: Places, task: str) -> int:
    """How many of a brief's threads wait for the designer."""
    return sum(1 for t in read(places, task)["threads"] if t["state"] == OPEN)
