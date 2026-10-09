"""The QA verbs (factory spec, "QA"): `factory qa plan`, `qa-ready`, `ready-check`, `qa result` and `qa-clean`.

The warden reads the branch with git as the agent, through the agent's own unit, and reads surfaces.toml from the
default branch, which only the owner changes, never from the worktree. A ready record names the full head the slot
runs; the guard reads the records from public/ to let testers start, and counts QA rounds by their heads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.qa import evidence, plan, qa_browser
from runtime.queue import tasks
from runtime.queue.formats import SHA
from runtime.queue.statuses import IMPL
from runtime.shared import store
from runtime.shared.models import RETIRED, Session, Task
from runtime.shared.protocol import Refused, ok
from runtime.slots import health, leases
from runtime.warden import doing, registry
from runtime.warden.context import Context

SURFACES_FILE = "check-values/surfaces.toml"


def _implementer(ctx: Context) -> Session:
    session = registry.read(ctx.places).get(ctx.caller.session)
    if session is None or session.role != IMPL or session.state == RETIRED:
        raise Refused("qa", "QA belongs to a live implementer's run")
    return session


def _git(ctx: Context, cwd: str, *argv: str) -> str:
    code, out = ctx.launcher.agent_command(["git", "-C", cwd, *argv], cwd)
    if code != 0:
        raise Refused("git", f"git {argv[0]} exited {code}: {out.strip()[-200:]}")
    return out.strip()


def _head(ctx: Context, session: Session) -> str:
    return _git(ctx, session.worktree, "rev-parse", "HEAD")


def _ready_path(ctx: Context, session: Session) -> Path:
    return ctx.places.state / "public" / "ready" / f"{session.id}.json"


def ready_records(ctx: Context, session: Session) -> list[dict[str, Any]]:
    raw = store.read_json_or(_ready_path(ctx, session), {"records": []})
    records = raw.get("records") if isinstance(raw, dict) else None
    if not isinstance(records, list):
        raise store.StoreError(f"{_ready_path(ctx, session)}: no records list")
    return records


def _results_path(ctx: Context, session: Session) -> Path:
    return ctx.places.state / "qa-results" / f"{session.id}.jsonl"


def testers_for(ctx: Context, session: Session, task: Task) -> dict[str, list[str]]:
    """The required testers for the worktree's head, each with why: the task's surfaces and the diff's, joined."""
    settings = ctx.settings()
    branch = settings.get_str("repo.default_branch")
    prefixes = plan.surface_prefixes(_git(ctx, settings.get_str("repo.path"), "show", f"{branch}:{SURFACES_FILE}"))
    changed = _git(ctx, session.worktree, "diff", "--name-only", "--no-renames", f"{branch}...HEAD").splitlines()
    rung = task.run if task.run is not None else session.rung
    if rung is None:
        raise Refused("qa", f"{task.id} has no run, so its testers are unknown")
    return plan.required(task.surfaces, rung, plan.diff_surfaces(changed, prefixes))


def qa_plan(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    session = _implementer(ctx)
    found = testers_for(ctx, session, tasks.read(ctx, session.task))
    return ok(head=_head(ctx, session), testers=[{"tester": t, "why": why} for t, why in found.items()])


def qa_ready(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Every slice is built and the slot runs the head: testers may start on it."""
    session = _implementer(ctx)
    asked, slices = args.get("head"), args.get("slices")
    if not isinstance(asked, str) or not SHA.match(asked) or not isinstance(slices, list) or not slices:
        raise Refused("qa-ready", "qa-ready names the head and one commit per slice: qa-ready <head> --slices …")
    if not all(isinstance(s, str) and SHA.match(s) for s in slices):
        raise Refused("qa-ready", "each slice is a commit sha")
    task = tasks.read(ctx, session.task)
    if task.slices and len(slices) != task.slices:
        raise Refused("qa-ready", f"{task.id} has {task.slices} slices; name one commit for each, not {len(slices)}")
    head = _head(ctx, session)
    if not head.startswith(asked):
        raise Refused("qa-ready", f"{asked[:9]} is not your branch's head ({head[:9]}): commit, then ask again")
    for commit in slices:
        code, _ = ctx.launcher.agent_command(
            ["git", "-C", session.worktree, "merge-base", "--is-ancestor", commit, head], session.worktree
        )
        if code != 0:
            raise Refused("qa-ready", f"{commit[:9]} is not on your branch inside {head[:9]}")
    lease = leases.held_by(ctx.places, ctx.settings(), session.name)
    if lease is None:
        raise Refused("slot", "claim a slot and rebuild it at your head first: factory slot claim, then slot up")
    running = health.running_commit(ctx, lease.slot, session.id)
    if running is None or not (head.startswith(running) or running.startswith(head)):
        raise Refused("qa-ready", f"your slot runs {running or 'nothing readable'}, not {head[:9]}: rebuild it")
    testers = testers_for(ctx, session, task)
    with ctx.lock:
        records = ready_records(ctx, session)
        record = {"head": head, "slices": slices, "at": ctx.clock.iso(), "slot": lease.slot, "testers": list(testers)}
        store.write_json(_ready_path(ctx, session), {"records": [*records, record]})
        rounds = len({str(r["head"]) for r in [*records, record]})
        doing.record(ctx.places, session.id, slice=len(slices), slices=len(slices), qa_round=rounds)
    if task.status == "building":
        tasks.move(ctx, task.id, "qa", "qa-ready", IMPL, f"ready for QA at {head[:9]}")
    return ok(head=head, slot=lease.slot, testers=[{"tester": t, "why": why} for t, why in testers.items()])


def ready_check(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """A tester's first step: the last ready record still holds, and the slot still runs its head."""
    session = _implementer(ctx)
    records = ready_records(ctx, session)
    if not records:
        raise Refused("ready-check", "not ready: no slices recorded")
    latest = records[-1]
    running = health.running_commit(ctx, int(latest["slot"]), session.id)
    head = str(latest["head"])
    if running is None or not (head.startswith(running) or running.startswith(head)):
        raise Refused("ready-check", f"not ready: the slot runs {running or 'nothing readable'}, not {head[:9]}")
    return ok(head=head, slot=latest["slot"], testers=latest.get("testers", []))


def _count(args: dict[str, Any], key: str) -> int:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Refused("qa-result", f"--{key} is a whole number from 0")
    return value


def qa_result(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """A tester's last step: its counts of open findings on the head it tested, and its runs into evidence."""
    session = _implementer(ctx)
    tester = registry.read(ctx.places).get(ctx.caller.agent_id) if ctx.caller.agent_id else None
    if tester is None or not tester.role.startswith("qa-"):
        raise Refused("qa-result", "only a registered tester reports a QA result")
    head = args.get("head")
    records = ready_records(ctx, session)
    if not isinstance(head, str) or not records or not str(records[-1]["head"]).startswith(head) or len(head) < 7:
        raise Refused("qa-result", "report on the head of the last ready record: --head <sha>")
    counts = {key: _count(args, key) for key in ("blockers", "majors", "minors")}
    full = str(records[-1]["head"])
    result = {"head": full, "tester": tester.role, "agent": ctx.caller.agent_id, "at": ctx.clock.iso(), **counts}
    store.append_jsonl(_results_path(ctx, session), result)
    verdict = (
        "clean"
        if not (counts["blockers"] or counts["majors"])
        else (f"{counts['blockers']} blockers, {counts['majors']} majors")
    )
    doing.record(ctx.places, ctx.caller.agent_id, verdict=verdict)
    taken = evidence.intake(
        ctx, qa_browser.runs_root(), session.id, session.task, full, tester.role, str(records[-1]["at"])
    )
    return ok(head=full, tester=tester.role, evidence=taken, **counts)


def qa_clean(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """Refused until every required tester reported on the final head with no open blocker or major."""
    session = _implementer(ctx)
    records = ready_records(ctx, session)
    if not records:
        raise Refused("qa-clean", "no ready record: run factory qa-ready first")
    head = str(records[-1]["head"])
    if _head(ctx, session) != head:
        raise Refused("qa-clean", f"your head moved past {head[:9]}: run qa-ready on it and test it again")
    latest: dict[str, dict[str, Any]] = {}
    for result in store.read_jsonl(_results_path(ctx, session)):
        if result["head"] == head:
            latest[str(result["tester"])] = result
    missing = [t for t in records[-1].get("testers", []) if t not in latest]
    open_ = [t for t, r in latest.items() if t in records[-1].get("testers", []) and (r["blockers"] or r["majors"])]
    said = ([f"no result from {', '.join(missing)}"] if missing else []) + (
        [f"open blockers or majors from {', '.join(open_)}"] if open_ else []
    )
    if said:
        raise Refused("qa-clean", f"not clean on {head[:9]}: {'; '.join(said)}")
    tasks.move(ctx, session.task, "qa-clean", "qa-clean", IMPL, f"QA clean at {head[:9]}")
    return ok(head=head, task=session.task)
