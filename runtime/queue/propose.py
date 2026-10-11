"""`factory propose`: the orchestrator puts a task on Home for the owner (routing spec, R4).

At most ``routing.max_proposals`` places hold proposals. A proposal carries a picture and a one-line why, and the
brief its run starts with once approved (the warden starts it; the orchestrator writes nothing more). It runs at
the rung ``factory route`` names unless it names another, which needs a reason that goes to the task's log. A
running task comes back one rung up the same way, with its approval gone. ``propose --none`` fills a place with
"nothing else".
"""

from __future__ import annotations

from typing import Any

from runtime.queue import pictures, proposals, route, statuses, tasks
from runtime.queue.book import Queue
from runtime.queue.formats import one_line
from runtime.shared import store
from runtime.shared.models import RETIRED, STOPPED, Session
from runtime.shared.protocol import Refused
from runtime.warden import lifecycle, registry
from runtime.warden.context import Context


def propose(
    ctx: Context, task_id: str, run: object, why: str, reason: str, picture: bytes | None, brief: str
) -> dict[str, Any]:
    settings = ctx.settings()
    if len(brief.encode()) > settings.get_int("queue.max_upload_kib") * 1024:
        raise Refused("brief", "the brief is too long; put the detail in the task")
    if run is not None and (isinstance(run, bool) or not isinstance(run, int) or run not in range(1, 5)):
        raise Refused("run", "a proposal runs at rung 1 to 4; a rung-0 task closes with a reason instead")
    if not why.strip() or "\n" in why.strip():
        raise Refused("why", "a proposal says in one line why now: --why '…'")
    data, kind = pictures.clean(picture, settings.get_int("queue.max_upload_kib"))
    queue = Queue.of(ctx)
    with ctx.lock, queue.lock():
        task = queue.task(task_id)
        if task.not_now:
            raise Refused("not-now", f"the owner said not now to {task_id}; it waits for a new request or a boost")
        planned = route.route(task, settings)
        if run is None:
            if planned == 0:
                raise Refused("run", f"factory route names rung 0 for {task_id}: close it with its reason instead")
            run = planned
        if run != planned and not reason.strip():
            raise Refused("route", f"factory route names rung {planned} for {task_id}; a run of {run} needs --reason")
        event = "propose" if task.status == "queued" else "rung-up"
        if event == "rung-up" and (task.run is None or run <= task.run):
            raise Refused("rung-up", f"{task_id} is {task.status}; it comes back only a rung up from {task.run}")
        before = proposals.read(ctx.places)
        if event == "propose":
            # A new proposal takes the first free place; a run that comes back a rung up goes to the top.
            found = proposals.read(ctx.places)
            proposals.place(found, task_id, settings.get_int("routing.max_proposals"))
            proposals.write(ctx.places, found)
        rel = pictures.task_picture(task_id, data, kind)
        said = f"rung {run}: {one_line(why)}" + (f" (route names {planned}: {one_line(reason)})" if reason else "")
        try:
            moved = tasks.transition(
                ctx,
                queue,
                task,
                "proposed",
                event,
                statuses.ORCH,
                said,
                {rel: data},
                run=run,
                why=one_line(why),
                picture=rel,
            )
        except BaseException:
            proposals.write(ctx.places, before)
            raise
        place = proposals.read(ctx.places).open.index(task_id)
        brief_file = lifecycle.brief_path(ctx, task_id)
        if brief.strip():
            store.write_text(brief_file, brief.strip() + "\n", mode=0o600)
        else:
            brief_file.unlink(missing_ok=True)
        stopped = _stop_runs(ctx, task_id, run) if event == "rung-up" else []
    # A run moved up a rung is done at the old one: its session stops (its branch stays) so it cannot work on.
    lifecycle.after_retire(ctx, stopped)
    return {"task": moved.id, "place": place, "run": run, "route": planned}


def _stop_runs(ctx: Context, task_id: str, run: int) -> list[Session]:
    """Retire the task's live implementer or designer, and what runs under it; what was retired."""
    sessions = lifecycle.read(ctx)
    retired: list[Session] = []
    for s in [s for s in sessions.values() if s.task == task_id and not s.parent and s.state != RETIRED]:
        retired += lifecycle.retire(ctx, registry.subtree(sessions, s.id), s, STOPPED, statuses.ORCH,
                                    f"its task moved up to rung {run}")  # fmt: skip
    if retired:
        registry.write(ctx.places, sessions)
    return retired


def propose_none(ctx: Context) -> dict[str, Any]:
    """Every free place says nothing else is worth proposing, until a proposal takes one."""
    with ctx.lock:
        found = proposals.read(ctx.places)
        limit = ctx.settings().get_int("routing.max_proposals")
        found.open = [proposals.NONE if held is None else held for held in found.open]
        found.open += [proposals.NONE] * (limit - len(found.open))
        if proposals.NONE not in found.open:
            raise Refused("max-proposals", f"routing.max_proposals ({limit}) proposals are open; no place is free")
        proposals.write(ctx.places, found)
    return {"open": found.open}
