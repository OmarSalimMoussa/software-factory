"""The QA verbs (factory spec, "QA"): `factory qa plan`, `qa-ready`, `ready-check`, `qa result` and `qa-clean`.

The warden reads the branch with git as the agent, through the agent's own unit, and reads surfaces.toml from the
default branch, which only the owner changes, never from the worktree. A ready record names the full head the slot
runs; the guard reads the records from public/ to let testers start, and counts QA rounds by their heads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from runtime.qa import evidence, findings, plan, qa_browser
from runtime.queue import tasks
from runtime.queue.formats import SHA
from runtime.queue.statuses import IMPL
from runtime.shared import store
from runtime.shared.models import RETIRED, Session, Task
from runtime.shared.protocol import Refused, ok
from runtime.slots import health, leases
from runtime.warden.context import Context
from runtime.warden.sessions import doing, registry

SURFACES_FILE = "check-values/surfaces.toml"


def _implementer(ctx: Context) -> Session:
    session = registry.read(ctx.places).get(ctx.caller.session)
    if session is None or session.role != IMPL or session.state == RETIRED:
        raise Refused("qa", "QA belongs to a live implementer's run")
    return session


# A slice's commit ends its message with this trailer: `Slice: 2/4`.
_SLICE = re.compile(r"(\d+)/\d+")


def _git(ctx: Context, cwd: str, *argv: str) -> str:
    code, out = ctx.launcher.agent_command(["git", "-C", cwd, *argv], cwd)
    if code != 0:
        raise Refused("git", f"git {argv[0]} exited {code}: {out.strip()[-200:]}")
    return out.strip()


def _head(ctx: Context, session: Session) -> str:
    return _git(ctx, session.worktree, "rev-parse", "HEAD")


def _ready_path(ctx: Context, session: Session) -> Path:
    return ctx.places.ready(session.id)


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
    """Every slice is built and the slot runs the head: testers may start on it. The task records the worktree's
    branch here: its merge turn names it, and the warden finds its merge by it."""
    session = _implementer(ctx)
    asked, slices = args.get("head"), args.get("slices")
    if not isinstance(asked, str) or not SHA.match(asked):
        raise Refused("qa-ready", "qa-ready names the head: qa-ready <head>")
    task = tasks.read(ctx, session.task)
    if slices is None:
        slices = _slice_commits(ctx, session, asked, task.slices)
    if not isinstance(slices, list) or not slices or not all(isinstance(s, str) and SHA.match(s) for s in slices):
        raise Refused("qa-ready", "each slice is a commit sha")
    if task.slices and len(slices) != task.slices:
        raise Refused("qa-ready", f"{task.id} has {task.slices} slices; name one commit for each, not {len(slices)}")
    head = _head(ctx, session)
    if not head.startswith(asked):
        raise Refused("qa-ready", f"{asked[:9]} is not your branch's head ({head[:9]}): commit, then ask again")
    branch = _git(ctx, session.worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        raise Refused("qa-ready", "your worktree is on no branch: commit on your own branch, then ask again")
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
        tasks.move(ctx, task.id, "qa", "qa-ready", IMPL, f"ready for QA at {head[:9]}", branch=branch)
    return ok(head=head, slot=lease.slot, testers=[{"tester": t, "why": why} for t, why in testers.items()])


def _slice_commits(ctx: Context, session: Session, head: str, count: int | None) -> list[str]:
    """The commit each slice ended with, found by its `Slice: <n>/<N>` trailer on the branch: the newest per slice,
    in slice order. Refused, naming what is missing, unless every slice of the task's count has one."""
    default = ctx.settings().get_str("repo.default_branch")
    log = _git(ctx, session.worktree, "log", "--format=%H%x09%(trailers:key=Slice,valueonly,separator=%x20)",
               f"{default}..{head}")  # fmt: skip
    found: dict[int, str] = {}
    for line in log.splitlines():
        commit, _, trailer = line.partition("\t")
        mark = _SLICE.fullmatch(trailer.strip())
        if mark and int(mark.group(1)) not in found:
            found[int(mark.group(1))] = commit
    want = count if count else max(found, default=0)
    missing = [str(n) for n in range(1, want + 1) if n not in found]
    if not want or missing:
        named = f"Slice: {', '.join(missing) or 1}/{want or 'N'}"
        raise Refused("qa-ready", f"no commit on your branch carries the trailer {named}; end each slice's commit "
                                  "with it, or name the commits with --slices")  # fmt: skip
    return [found[n] for n in range(1, want + 1)]


def _touched(ctx: Context, session: Session, task: Task, tested: str, head: str) -> set[str]:
    """The testers the commits from ``tested`` to ``head`` require: those whose surfaces they touch."""
    settings = ctx.settings()
    surfaces = f"{settings.get_str('repo.default_branch')}:{SURFACES_FILE}"
    prefixes = plan.surface_prefixes(_git(ctx, settings.get_str("repo.path"), "show", surfaces))
    changed = _git(ctx, session.worktree, "diff", "--name-only", "--no-renames", tested, head).splitlines()
    rung = task.run if task.run is not None else session.rung
    return set(plan.required([], rung or 0, plan.diff_surfaces(changed, prefixes)))


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


def _tester_on_head(ctx: Context, args: dict[str, Any], verb: str) -> tuple[Session, Session, str]:
    """The implementer, its registered tester that asks, and the full head of the last ready record it names."""
    session = _implementer(ctx)
    tester = registry.read(ctx.places).get(ctx.caller.agent_id) if ctx.caller.agent_id else None
    if tester is None or not tester.role.startswith("qa-"):
        raise Refused(verb, "only a registered tester reports a QA finding or result")
    head = args.get("head")
    records = ready_records(ctx, session)
    if not isinstance(head, str) or not records or not str(records[-1]["head"]).startswith(head) or len(head) < 7:
        raise Refused(verb, "report on the head of the last ready record: --head <sha>")
    return session, tester, str(records[-1]["head"])


def qa_finding(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """A tester records one finding on the head it tests, as it finds it."""
    session, tester, full = _tester_on_head(ctx, args, "qa-finding")
    finding = findings.add(ctx, session.task, full, tester.role, ctx.caller.agent_id, args, data)
    return ok(finding=finding, head=full)


def qa_waive(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """The orchestrator's ruling on a disputed finding: it no longer counts, for the reason given."""
    task, finding = args.get("task"), args.get("finding")
    if not isinstance(task, str) or not isinstance(finding, str):
        raise Refused("qa-waive", "factory qa waive <task> <finding> --why '…'")
    findings.waive(ctx, task, finding, str(args.get("why", "")))
    return ok(task=task, waived=finding)


def qa_result(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """A tester's last step: its verdict on the head it tested, counted from the findings it recorded, and its runs
    into evidence."""
    session, tester, full = _tester_on_head(ctx, args, "qa-result")
    records = ready_records(ctx, session)
    found = findings.counts(ctx, session.task, full, agent=ctx.caller.agent_id)
    counts = {"blockers": found["blocker"], "majors": found["major"], "minors": found["minor"]}
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
    """Refused until every required tester has a verdict on the final head with no open blocker or major. A clean
    verdict on an earlier head carries forward when the commits since it touch none of that tester's surfaces."""
    session = _implementer(ctx)
    records = ready_records(ctx, session)
    if not records:
        raise Refused("qa-clean", "no ready record: run factory qa-ready first")
    head = str(records[-1]["head"])
    if _head(ctx, session) != head:
        raise Refused("qa-clean", f"your head moved past {head[:9]}: run qa-ready on it and test it again")
    task = tasks.read(ctx, session.task)
    latest: dict[str, str] = {}  # each tester's newest head with a verdict
    for result in store.read_jsonl(_results_path(ctx, session)):
        latest[str(result["tester"])] = str(result["head"])
    missing: list[str] = []
    open_: list[str] = []
    for tester in records[-1].get("testers", []):
        tested = latest.get(tester)
        if tested is None or (tested != head and tester in _touched(ctx, session, task, tested, head)):
            missing.append(tester)
        elif findings.open_count(ctx, task.id, tested, tester):
            open_.append(tester)
    said = ([f"no result from {', '.join(missing)} on what changed since it tested"] if missing else []) + (
        [f"open blockers or majors from {', '.join(open_)}"] if open_ else []
    )
    if said:
        raise Refused("qa-clean", f"not clean on {head[:9]}: {'; '.join(said)}")
    tasks.move(ctx, session.task, "qa-clean", "qa-clean", IMPL, f"QA clean at {head[:9]}")
    return ok(head=head, task=session.task)
