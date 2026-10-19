"""Who may start, and on what: the owner's approval (or the warden's own outage record), the vitals, the caps, the
model the approved rung names, never the factory's own repo, and never while a switch of Claude accounts restarts
the agents' daemon.

Every check raises Refused with what to do instead. The queue's tasks and statuses and the slots' leases belong to
their own modules; the calls into them sit at the bottom of this file, where a test replaces them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.queue import approvals, statuses, tasks
from runtime.queue import route as ladders
from runtime.queue.formats import FormatError
from runtime.shared import order, store
from runtime.shared.models import DEPRIORITIZED, RETIRED, WAITING, WORKING, Session, Task
from runtime.shared.protocol import Refused
from runtime.shared.settings import Settings
from runtime.slots import leases
from runtime.warden.context import Context
from runtime.warden.delivery import outage as outage_record
from runtime.warden.owner import ops
from runtime.warden.sessions import registry
from runtime.warden.spend import cost

ORCH, IMPL, DESIGN = "orchestrator", "implementer", "designer"
FAMILY = {ORCH: "orch", IMPL: "impl", DESIGN: "design"}
CAP = {IMPL: "caps.implementers", DESIGN: "caps.designers"}
# The factory never works on itself. This is code, not a setting, so no saved setting can turn it off.
FACTORY_REPO = "software-factory"
SEVERE = "severe"
# Where a switch of Claude accounts stands: its daemon restarting, or back (runtime/warden/owner/accounts.py).
RESTARTING, BACK = "restarting", "back"
# owner/name at the end of any git remote or GitHub name: git@host:owner/name.git, https://host/owner/name, owner/name.
_REMOTE = re.compile(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/*$")


@dataclass(frozen=True, slots=True)
class Permit:
    role: str
    model: str
    effort: str
    rung: int
    bypass: bool  # started under the outage bypass, with no approval


def at_work(sessions: Iterable[Session]) -> bool:
    """Whether the orchestrator is at work, working or waiting: a pause benches it and the owner's stop retires it,
    and only an orchestrator at work chooses proposals or has runs started."""
    return any(s.role == ORCH and s.state in (WORKING, WAITING) and not s.parent for s in sessions)


def actor(ctx: Context) -> str:
    return "owner" if ctx.caller.kind in ("owner", "dashboard") else ctx.caller.role


def approved_order(ctx: Context) -> list[str]:
    raw = store.read_json_or(ctx.places.proposals, {"approved": []})
    approved = raw.get("approved") if isinstance(raw, dict) else None
    if not isinstance(approved, list):
        raise Refused("state", f"{ctx.places.proposals} holds no approved list; the warden's state needs a look")
    return [str(t) for t in approved]


def ranking(ctx: Context) -> order.Ranking:
    """What the one order reads: the owner's order among approved runs and the goal items' ranks."""
    return order.Ranking(approved_order(ctx), tasks.goal_ranks(ctx))


def vitals_level(ctx: Context) -> tuple[str, str]:
    from runtime.warden.health import vitals  # vitals reaches this module through the clean-up and the cards

    try:
        raw = store.read_json(ctx.places.vitals)
    # telemetry: expected — no vitals yet: none has been read, so the level is unknown
    except FileNotFoundError:
        return vitals.UNKNOWN, "none read yet"
    except store.StoreError as exc:
        raise Refused("vitals", f"the vitals cannot be read ({exc}); see factory status") from exc
    level, why = (raw.get("level"), raw.get("why")) if isinstance(raw, dict) else (None, None)
    readings = raw.get("readings") if isinstance(raw, dict) else None
    if level not in vitals.LEVELS or not isinstance(why, str) or not isinstance(readings, list):
        raise Refused("vitals", "the vitals record names no level and readings; see factory status")
    if level != vitals.RED:
        return str(level), why
    try:
        return vitals.install_again(level, why, readings, ctx.places)
    except (TypeError, KeyError) as exc:
        raise Refused("vitals", "the vitals record's readings cannot be read; see factory status") from exc


def check_vitals(ctx: Context, severe: bool) -> None:
    """Red blocks every start; amber, or vitals not read yet, block all but severe work. So does a day whose known
    spend, at API prices, reached cost.daily_budget_usd."""
    from runtime.warden.health import vitals

    level, why = vitals_level(ctx)
    if level == vitals.RED:
        raise Refused("vitals", f"the machine's vitals are red ({why}), so nothing starts")
    if level != vitals.GREEN and not severe:
        raise Refused("vitals", f"the machine's vitals are {level} ({why}), so only severe work starts")
    budget = ctx.settings().get_float("cost.daily_budget_usd")
    spent = cost.spent_days(ctx).get(ctx.clock.iso()[:10], [0.0, 0])[0]
    if spent >= budget and not severe:
        raise Refused("budget", f"today's spend is ${spent:,.0f} of the ${budget:,.0f} budget (cost.daily_budget_usd), "
                                "so only severe work starts until the UTC day ends")  # fmt: skip


def switch_path(ctx: Context) -> Path:
    return ctx.places.state / "account-switch.json"


def switching(ctx: Context) -> dict[str, Any] | None:
    """The switch of Claude accounts under way, or None. A record whose operation has ended was left by a switch
    the warden's own restart cut short (ops.recover marked it failed), and counts for nothing."""
    try:
        record = store.read_json(switch_path(ctx))
    # telemetry: expected — no record: no switch of accounts is under way
    except FileNotFoundError:
        return None
    except store.StoreError as exc:
        raise Refused("state", f"the account switch's record cannot be read ({exc}); the state needs a look") from exc
    try:
        op = ops.get(ctx, {"id": record["op"]}, None)["op"]
    # telemetry: expected — the operation's file was pruned, so its switch is long over
    except Refused:
        return None
    return dict(record) if op["state"] == ops.RUNNING else None


def check_daemon(ctx: Context) -> None:
    """Nothing starts or resumes while a switch of Claude accounts restarts the daemon that would host it."""
    record = switching(ctx)
    if record is not None and record["daemon"] != BACK:
        raise Refused("switching", "the factory is switching Claude accounts; try again in a minute")


def check_cap(settings: Settings, sessions: Iterable[Session], role: str) -> None:
    """Working and waiting sessions hold a place (and those between states); deprioritized and retired ones do not."""
    held = registry.seated(sessions, role)
    cap = settings.get_int(CAP[role])
    if len(held) >= cap:
        names = ", ".join(s.name for s in held)
        raise Refused("cap", f"{len(held)} of {cap} {role}s hold a place ({names}); the warden benches one only "
                            "for severe work or a higher goal item, so this run waits for a place")  # fmt: skip


def cap_full(settings: Settings, sessions: Iterable[Session], role: str) -> bool:
    return len(registry.seated(sessions, role)) >= settings.get_int(CAP[role])


def check_revive_order(ctx: Context, sessions: dict[str, Session], role: str, task: Task, skip: str = "") -> None:
    """Deprioritized sessions come back before new runs behind them in the seat order, and in that order."""
    approved = ranking(ctx)
    for other in sessions.values():
        if other.id == skip or other.role != role or other.state != DEPRIORITIZED or other.parent:
            continue
        if not has_work(ctx, other):
            continue
        if order.seat_rank(read_task(ctx, other.task), approved) < order.seat_rank(task, approved):
            raise Refused("revive-first", f"{other.name} comes back first: it is ahead of {task.id}, and the warden "
                                          "revives it when a place frees")  # fmt: skip


def is_factory_repo(settings: Settings, remote: str) -> bool:
    """Whether a remote, or an owner/name, is the factory's own repo: the one factory.github names, or any repo
    named as the factory is (a fork or a copy of it)."""
    found = _REMOTE.search("/" + remote.strip())
    if found is None:
        return False
    owner_name = found.group(1).lower()
    own = settings.get_str("factory.github").lower()
    return (bool(own) and owner_name == own) or owner_name.rsplit("/", 1)[-1] == FACTORY_REPO


def check_not_factory(ctx: Context) -> None:
    """Refuse when the product's repo, by its setting or by its checkout's remote, is the factory's own."""
    settings = ctx.settings()
    repo = settings.get_str("repo.path")
    code, out = ctx.launcher.agent_command(["git", "-C", repo, "remote", "get-url", "origin"], repo)
    if code != 0:
        raise Refused("repo", f"the repo's remote cannot be read: {out.strip()[-200:]}; check repo.path")
    if any(is_factory_repo(settings, remote) for remote in (settings.get_str("repo.github"), out.strip())):
        raise Refused("own-repo", "the factory never works on its own repo; point repo.path at a product")


def model_for(settings: Settings, role: str, rung: int, slices: int | None) -> tuple[str, str]:
    """The model and effort a rung's session runs: a designer, or the implementer for the rung or the spec's size."""
    if role == DESIGN:
        return settings.model("designer")
    if rung >= 3:
        if slices is None:
            raise Refused("rung", "the spec names no slice count, so the implementer's size is unknown; set slices")
        rung = 1 if slices <= settings.get_int("routing.small_max_slices") else 2
    return settings.model("implementer_small" if rung == 1 else "implementer_large")


def check_start(ctx: Context, sessions: dict[str, Session], role: str, task: Task, outage: bool) -> Permit:
    """The permit for a new implementer or designer on ``task``; raises Refused naming the first rule it breaks.

    Hold ctx.lock from here through the launch: an outage permit is recorded only once its session is started.
    """
    settings = ctx.settings()
    if role not in (IMPL, DESIGN):
        raise Refused("role", "a start is for an implementer or a designer")
    live = [s for s in sessions.values() if s.task == task.id and s.role == role and s.state != RETIRED]
    if live:
        raise Refused("one-run", f"{task.id} already has {live[0].name} ({live[0].state}); the warden "
                                  "brings a benched one back itself, or stop it first")  # fmt: skip
    if task.severity != SEVERE and not task.goal_item:
        raise Refused("goal", f"{task.id} names no goal item, and only severe work starts without one")
    if outage:
        if role != IMPL or task.run not in (1, 2):
            raise Refused("outage", "the outage bypass starts one implementer at rung 1 or 2, never a designer")
        if not outage_record.bypass_allowed(ctx):
            raise Refused("outage", "no outage bypass is open: prod is up or the fix already started; propose it")
        model, effort = model_for(settings, role, task.run, task.slices)
        return Permit(role, model, effort, task.run, bypass=True)
    return _approved(ctx, settings, role, task)


def _approved(ctx: Context, settings: Settings, role: str, task: Task) -> Permit:
    """The permit the owner's approval of ``task`` gives, while its facts, status and model still match it."""
    approval = approvals.current(ctx.places, task.id)
    if approval is None:
        raise Refused("approval", f"the owner has not approved {task.id}; propose it")
    if not approvals.matches(approval, task):
        raise Refused("approval", f"{task.id} changed since the owner approved it; propose it again")
    rung = approval.rung
    # The facts that made the approved rung allowed (slices, surfaces, owner class, cause) may have changed since:
    # the ladder runs again, and a route higher than the one proposed and approved refuses. A run proposed below its
    # route, with the orchestrator's reason, still starts: its facts did not move.
    needed = ladders.route(task, settings)
    if needed > approval.routed:
        raise Refused("rung", f"{task.id}'s facts changed since the owner approved it: they route to rung {needed} "
                              f"now, not {approval.routed}; propose it again")  # fmt: skip
    designed = rung >= 3 and role == IMPL
    if role == DESIGN and rung < 3:
        raise Refused("rung", f"the owner approved rung {rung}, which needs no designer; start an implementer")
    want = "building" if designed else "approved"
    if task.status != want:
        raise Refused("status", f"{task.id} is {task.status}; this start needs it {want}")
    if designed and rung == 4 and not approvals.has_go(ctx.places, task.id):
        raise Refused("go", f"{task.id}'s spec waits for the owner's Go before its build starts")
    model, effort = model_for(settings, role, rung, task.slices)
    if not designed and approval.model != model:
        raise Refused(
            "approval", f"the owner approved {approval.model}, but rung {rung} runs {model} now; propose again"
        )
    return Permit(role, model, effort, rung, bypass=False)


def needed_role(ctx: Context, task_id: str) -> str:
    """The role a task's next session takes: a designer for an approved rung 3 or 4, else an implementer."""
    task = read_task(ctx, task_id)
    if task.status == "building":
        return IMPL
    approval = approvals.current(ctx.places, task.id)
    rung = approval.rung if approval is not None else task.run
    return DESIGN if rung is not None and rung >= 3 else IMPL


def benchable(ctx: Context, sessions: dict[str, Session], role: str, for_task: str) -> list[Session]:
    """Who a bench for ``for_task`` may push out, waiting before working, then last in the seat order first: never one
    in its merge turn, and a working one only after its minimum run time, unless the new task is severe."""
    new, approved = read_task(ctx, for_task), ranking(ctx)
    least = 0 if new.severity == SEVERE else ctx.settings().get_int("watch.bench_min_run_minutes") * 60
    found = [(s, read_task(ctx, s.task)) for s in registry.seated(sessions.values(), role)
             if s.state in (WORKING, WAITING) and not s.merge_turn and s.task != for_task
             and (s.state == WAITING or ctx.clock.age(s.since) >= least)]  # fmt: skip
    found = [(s, t) for s, t in found if order.may_bench(new, t, approved)]
    found.sort(key=lambda pair: order.seat_rank(pair[1], approved), reverse=True)
    found.sort(key=lambda pair: pair[0].state != WAITING)  # stable: keeps the rank order within each state
    return [s for s, _ in found]


def in_order(ctx: Context, sessions: list[Session]) -> list[Session]:
    """The orchestrator first, then the rest in their tasks' seat order."""
    approved = ranking(ctx)
    ranked = sorted((s for s in sessions if s.task), key=lambda s: order.seat_rank(read_task(ctx, s.task), approved))
    return [s for s in sessions if not s.task] + ranked


# ---- the queue's tasks and the slots' leases ----------------------------------------------------------------------


def has_work(ctx: Context, s: Session) -> bool:
    """Whether a benched session's task has work for it: one benched at its merge waits for the task's return."""
    return bool(s.task) and read_task(ctx, s.task).status not in statuses.WAITS


def read_task(ctx: Context, task_id: str) -> Task:
    try:
        return tasks.read(ctx, task_id)
    except FileNotFoundError as exc:
        raise Refused("task", f"no task {task_id}") from exc
    except (FormatError, UnicodeDecodeError) as exc:
        raise Refused("task", f"{task_id} cannot be read ({exc}); the queue-format sweep reports it as the "
                              "warden's fault") from exc  # fmt: skip


def move_task(ctx: Context, task_id: str, to: str, event: str, by: str = statuses.WARDEN) -> None:
    tasks.move(ctx, task_id, to, event, by)


def claim_slot(ctx: Context, session: Session) -> int:
    return leases.claim(ctx, session.name, "revived")


def free_slot(ctx: Context, slot: int) -> None:
    leases.release(ctx, slot)
