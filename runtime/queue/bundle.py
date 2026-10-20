"""Merge-check bundles: the merges that wait for their check of the default branch, checked together.

A merge waits from when the warden finds it until a check clears it, its task at merged; a task merged twice waits by
its newest merge. A bundle is the oldest waiting merges, at most rounds.bundle_merges, walked at one commit: the one
just before the next merge after the bundle's last, or the tip, so a revert after its last merge is in it. Its
scenarios are the standing ones whose `protects` names a path one of its merges changed, read at the tip; a tester
reads each as the build slot 0 runs has it (runtime/qa/walked.py), unless an earlier full pass carries to it
(runtime/qa/carry.py).

A saved check of a commit on the default branch's first-parent line clears every merge waiting up to it, at once, when
every row passed, each scenario those merges protect has a row or carries, slot 0's last probe named that commit if a
tester walked it, and they are at most rounds.bundle_merges, or the check of the first that many failed. Anything
less clears none, and each row that did not pass says what comes next (`_next`). Once the check of the first that
many did not pass, a fix joining it or slot 0 unable to come up there, the bundle runs through the tip. While the cap
of merges waits, a merge turn goes only to a fix: a task that changes a path a scenario protects whose newest row,
walked with them in, failed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from runtime.qa import carry, scenarios, walked
from runtime.qa.carry import PASS
from runtime.queue import statuses, tasks, turn
from runtime.queue.book import Queue
from runtime.queue.records import CheckRecord, ScenarioRow
from runtime.shared.models import Task
from runtime.shared.protocol import Refused, ok
from runtime.warden.context import Context
from runtime.warden.delivery import deploys
from runtime.warden.health import probes

BLOCKED = "blocked"
_MERGE = re.compile(r"^merge ([0-9a-f]{40})$")  # the line `git log --format='merge %H'` opens each merge with
_NO_BUILD = "if slot 0 cannot come up there, save that check with each scenario blocked and why"


@dataclass(frozen=True, slots=True)
class Merge:
    commit: str
    task: str
    walk_at: str  # where a check of it walks: it, and every waiting merge before it
    scenarios: frozenset[str]


@dataclass(frozen=True, slots=True)
class Line:
    """The default branch's first-parent line, tip first, and the merges on it waiting for their check, oldest first."""

    commits: list[str]
    at: dict[str, int]  # each commit's place on the line: 0 is the tip, a larger one is older
    waiting: list[Merge]
    standing: dict[str, scenarios.Scenario]  # the standing scenarios a tester may walk now, by name

    def upto(self, commit: str) -> list[Merge]:
        return [m for m in self.waiting if self.at[m.commit] >= self.at[commit]]

    @property
    def base(self) -> str:
        """The commit just before the oldest waiting merge: the last one with nothing waiting in it."""
        return self.commits[self.at[self.waiting[0].commit] + 1]


def line(ctx: Context) -> Line:
    settings = ctx.settings()
    default = settings.get_str("repo.default_branch")
    listed = turn.git(ctx, "rev-list", "--first-parent", "--parents", f"refs/heads/{default}")
    rows = [row.split() for row in listed.splitlines()]
    commits = [row[0] for row in rows]
    at = {commit: n for n, commit in enumerate(commits)}
    merge_places = [n for n, row in enumerate(rows) if len(row) > 2]
    merged = {t.id for t in Queue.of(ctx).tasks()[0] if t.status == turn.MERGED}
    newest: dict[str, str] = {}
    for commit, task in turn.merges(ctx.places).items():
        if task in merged and commit in at and (task not in newest or at[commit] < at[newest[task]]):
            newest[task] = commit
    order = sorted(newest, key=lambda task: -at[newest[task]])
    standing = walked.standing(ctx, commits[0], settings.get_int("qa.max_steps"), settings.get_int("qa.max_items"))
    changed = _changed(ctx, [newest[task] for task in order])
    waiting = []
    for task in order:
        commit = newest[task]
        later = [n for n in merge_places if n < at[commit]]
        guards = {
            name for name, s in standing.items() for path in changed[commit] if scenarios.covers(s.protects, path)
        }
        waiting.append(Merge(commit, task, commits[max(later) + 1] if later else commits[0], frozenset(guards)))
    return Line(commits, at, waiting, standing)


def _changed(ctx: Context, commits: list[str]) -> dict[str, list[str]]:
    if not commits:
        return {}
    out = turn.git(
        ctx, "log", "--no-walk=unsorted", "--diff-merges=first-parent", "--name-only", "--format=merge %H", *commits
    )
    changed: dict[str, list[str]] = {commit: [] for commit in commits}
    current = ""
    for row in out.splitlines():
        opened = _MERGE.match(row)
        if opened:
            current = opened[1]
        elif row and current:
            changed[current].append(row)
    return changed


def protected(found: Line, commit: str) -> frozenset[str]:
    """The standing scenarios the merges waiting up to ``commit`` protect."""
    return frozenset[str]().union(*(m.scenarios for m in found.upto(commit)))


def on_line(ctx: Context, found: Line, commit: str) -> None:
    if commit not in found.at:
        default = ctx.settings().get_str("repo.default_branch")
        raise Refused(
            "check",
            f"{commit[:12]} is not on {default}'s first-parent line: a check of the default "
            "branch names a commit on it",
        )


def settle(ctx: Context, found: Line, record: CheckRecord) -> dict[str, Any]:
    """What a saved check clears, at once, or why it clears none; each row that did not pass says what comes next."""
    members = found.upto(record.commit)
    failing = [row for row in record.rows if row.result != PASS]
    checks, _ = Queue.of(ctx).checks()
    if failing:
        need = ctx.settings().get_int("rounds.revert_fails")
        return {"cleared": [], "next": [_next(found, checks, record, row, need) for row in failing]}
    first = _past_cap(ctx, found, checks, members)
    if first:
        cap = ctx.settings().get_int("rounds.bundle_merges")
        why = f"{len(members)} merges wait up to {record.commit[:12]}, past the cap of {cap}: check the oldest {cap} "
        return {"cleared": [], "why": why + f"at {first[:12]} first (factory queue bundle); {_NO_BUILD}"}
    covered = {row.scenario for row in record.rows} | {row.scenario for row in record.carried}
    missing = sorted(protected(found, record.commit) - covered)
    if missing:
        return {
            "cleared": [],
            "why": f"no row for {', '.join(missing)}, which the merges up to {record.commit[:12]} "
            "protect: walk them there and save the check again",
        }
    if members and record.rows and not _slot0_ran(ctx, record.commit):
        return {
            "cleared": [],
            "why": f"slot 0's last probe does not name {record.commit[:12]}: a check clears merges "
            "only at the commit slot 0 runs, so save it before slot 0 moves",
        }
    for merge in members:
        note = f"the check of {record.commit[:12]} passed, for every merge waiting up to it"
        tasks.move(ctx, merge.task, turn.CHECKED, "checked", statuses.WARDEN, note)
    return {"cleared": [merge.task for merge in members]}


def _past_cap(ctx: Context, found: Line, checks: list[CheckRecord], members: list[Merge]) -> str:
    """Where the first rounds.bundle_merges walk, when ``members`` go past them and that check has not failed."""
    cap = ctx.settings().get_int("rounds.bundle_merges")
    first = found.waiting[cap - 1].walk_at if len(members) > cap else ""
    return first if first and not _failed(checks, first) else ""


def _slot0_ran(ctx: Context, commit: str) -> bool:
    last = probes.recent(ctx.places, probes.STAGING, 1)
    return bool(last) and last[0].ok and bool(last[0].commit) and commit.startswith(last[0].commit)


def _next(found: Line, checks: list[CheckRecord], record: CheckRecord, row: ScenarioRow, need: int) -> dict[str, Any]:
    """What a row that did not pass asks for: walk it again or alone, revert one merge once ``need`` walks with it in
    failed, or wait for a fix."""
    said = {"scenario": row.scenario, "result": row.result}
    members = found.upto(record.commit)
    if row.result == BLOCKED:
        return {**said, "do": "again", "why": "walk it again once what blocked it is fixed: a block is never a revert"}
    if row.scenario not in found.standing:
        return {**said, "do": "read", "why": "it is no standing scenario, so no merge's protects names it"}
    if not members:
        return {**said, "do": "request", "why": "no merge waits up to this commit: file what failed as a request"}
    here = found.at[record.commit]
    walks = [
        (found.at[c.commit], r.result)
        for c in checks
        if c.id != record.id and c.commit in found.at and found.at[c.commit] >= here
        for r in c.rows
        if r.scenario == row.scenario
    ]
    floor = _floor(found, members, walks, here)
    passes = [at for at, result in walks if result == PASS]
    if passes and min(passes) <= floor:
        fails = [here, *(at for at, result in walks if result not in (PASS, BLOCKED))]
        return {**said, **_since_pass(found, members, row.scenario, (min(passes), here), fails, need)}
    oldest = (
        "before every merge waiting" if floor == found.at[found.base] else "the oldest commit it was not blocked at"
    )
    if floor == here or any(at == floor and result not in (PASS, BLOCKED) for at, result in walks):
        return {
            **said,
            "do": "wait",
            "why": f"it fails at {found.commits[floor][:12]} too, {oldest}: its fix takes the next merge turn and "
            "joins the bundle",
        }
    return {
        **said,
        "do": "walk",
        "at": found.commits[floor],
        "why": "it has not passed since before the merges waiting: walk it alone there first",
    }


def _floor(found: Line, members: list[Merge], walks: list[tuple[int, str]], here: int) -> int:
    """The oldest place a walk can tell it apart: the base, or the oldest newer than every walk place where it only
    came back blocked, as slot 0 cannot come up there nor at any older commit; at last the check's own."""
    places = [p for p in (found.at[c] for c in [found.base, *(m.walk_at for m in members)]) if p > here]
    walked = {at for at, _ in walks}
    stuck = [p for p in places if p in walked and all(r == BLOCKED for at, r in walks if at == p)]
    return next((p for p in places if not stuck or p < min(stuck)), here)


def _since_pass(
    found: Line, members: list[Merge], scenario: str, places: tuple[int, int], fails: list[int], need: int
) -> dict[str, Any]:
    """Its suspects: the merges since its pass, those that protect it if any do. ``places`` are its pass's and the
    check's; ``fails``, where it failed since."""
    passed, here = places
    since = [m for m in members if found.at[m.commit] < passed]
    if passed == here or not since:
        return {"do": "again", "why": "it passed with every merge up to here in it: walk it again"}
    suspects = [m for m in since if scenario in m.scenarios] or since
    if len(suspects) == 1:
        return _one_suspect(found, suspects[0], found.commits[passed], fails, need)
    middle = suspects[(len(suspects) + 1) // 2 - 1]
    return {
        "do": "walk",
        "at": middle.walk_at,
        "suspects": [m.task for m in suspects],
        "why": "walk it alone there: a pass leaves the later suspects, a fail the earlier",
    }


def _one_suspect(found: Line, suspect: Merge, passed: str, fails: list[int], need: int) -> dict[str, Any]:
    """A revert once ``need`` walks with the suspect in failed: one fail of a scenario that sometimes fails is no
    proof."""
    seen = sum(1 for at in fails if at <= found.at[suspect.commit])
    if seen < need:
        return {
            "do": "walk",
            "at": suspect.walk_at,
            "suspects": [suspect.task],
            "why": f"{seen} of the {need} failed walks a revert needs have the one merge that could break it in: "
            "walk it alone there again",
        }
    return {
        "do": "revert",
        "merge": suspect.commit,
        "task": suspect.task,
        "why": f"the one merge since its pass at {passed[:12]} that could break it, failed {seen} times",
    }


def _newest(checks: list[CheckRecord], commit: str) -> CheckRecord | None:
    at = [c for c in checks if c.commit == commit]
    return max(at, key=lambda c: c.at) if at else None


def _failed(checks: list[CheckRecord], commit: str) -> bool:
    newest = _newest(checks, commit)
    return newest is not None and any(row.result != PASS for row in newest.rows)


def _longest(found: Line, checks: list[CheckRecord]) -> str | None:
    """The standing scenario whose last pass is oldest, or that never passed."""
    last: dict[str, int] = {}
    for check in checks:
        for row in check.rows:
            if row.result == PASS and check.commit in found.at:
                last[row.scenario] = min(last.get(row.scenario, len(found.commits)), found.at[check.commit])
    never = len(found.commits)
    return min(found.standing, key=lambda name: (-last.get(name, never), name)) if found.standing else None


def show(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """`factory queue bundle [--through <commit>]`: the merges to check together, where, and which scenarios."""
    found = line(ctx)
    if not found.waiting:
        return ok(merges=[], message="no merge waits for its check")
    cap = ctx.settings().get_int("rounds.bundle_merges")
    checks, _ = Queue.of(ctx).checks()
    members, walk_at = found.waiting[:cap], found.waiting[:cap][-1].walk_at
    if not args.get("through") and len(found.waiting) > cap and _failed(checks, walk_at):
        members, walk_at = found.waiting, found.commits[0]
    elif args.get("through"):
        walk_at = turn.full_commit(ctx, str(args["through"]))
        on_line(ctx, found, walk_at)
        members = found.upto(walk_at)
        if not members:
            oldest = found.waiting[0].commit[:12]
            raise Refused("bundle", f"no merge waits up to {walk_at[:12]}; the oldest waiting is {oldest}")
        first = _past_cap(ctx, found, checks, members)
        if first:
            grows = f"a bundle grows past it only once the check of the first {cap}, at {first[:12]}, failed"
            raise Refused("bundle", f"{len(members)} merges is past the cap of {cap}: {grows}; {_NO_BUILD}")
    names = sorted(frozenset[str]().union(*(m.scenarios for m in members)))
    carried = carry.carried(ctx, found, checks, names, walk_at)
    walk = [s for s in names if s not in carried.rows]
    tasks_of = {s: [m.task for m in members if s in m.scenarios] for s in names}
    answer: dict[str, Any] = {
        "merges": [{"commit": m.commit, "task": m.task} for m in members],
        "walk_at": walk_at,
        "base": found.base,
        "scenarios": [{"name": s, "items": found.standing[s].items, "merges": tasks_of[s]} for s in walk],
        "carried": [{"name": s, "check": c.check, "commit": c.commit} for s, c in sorted(carried.rows.items())],
        "waiting": len(found.waiting),
        **({"carry_off": carried.off} if carried.off else {}),
    }
    if not names:
        answer["walk_instead"] = _longest(found, checks)
    newest = _newest(checks, walk_at)
    if newest is not None:
        answer["check"] = newest.id
        need = ctx.settings().get_int("rounds.revert_fails")
        answer["next"] = [_next(found, checks, newest, row, need) for row in newest.rows if row.result != PASS]
    told = f"{len(members)} of {len(found.waiting)} waiting merges: "
    if names and not walk:
        told += "every scenario they protect carries an earlier full pass, so no tester walks: "
        return ok(**answer, message=told + f"factory queue check {walk_at[:12]} with no --body saves the check")
    up = "factory slot up" if walk_at == found.commits[0] else f"factory slot up --at {walk_at[:12]}"
    items = sum(found.standing[s].items for s in walk or [answer["walk_instead"]] if s in found.standing)
    scope = f"{len(walk)} scenarios" if names else f"{answer['walk_instead']}, as no scenario protects them"
    more = f" ({len(carried.rows)} more carry an earlier full pass)" if carried.rows else ""
    told += f"{up}, walk {scope}{more}, then factory queue check {walk_at[:12]}; one brief for them all says "
    off = f"; {carried.off}" if carried.off else ""
    return ok(**answer, message=told + f"Scenarios and checks: {items}, plus one for each check it adds{off}")


def view(ctx: Context, args: dict[str, Any]) -> dict[str, Any]:
    """The dashboard's reading: each waiting merge, oldest first, with the newest saved check whose walk had it in and
    whether slot 0's build has it in, None while which build slot 0 runs is not known; slot 0's build, on the line;
    and the cap of a bundle. With no task merged, it reads no git: the snapshot holds the warden's lock."""
    cap = ctx.settings().get_int("rounds.bundle_merges")
    if not any(t.status == turn.MERGED for t in Queue.of(ctx).tasks()[0]):
        return {"waiting": [], "staging": None, "cap": cap}
    found = line(ctx)
    checks, _ = Queue.of(ctx).checks()
    current = deploys.builds(ctx.places, probes.STAGING)["current"]
    staging = None if current is None else next((c for c in found.commits if c.startswith(current["commit"])), None)
    waiting = []
    for merge in found.waiting:
        place = found.at[merge.commit]
        walked = [c for c in checks if c.commit in found.at and found.at[c.commit] <= place]
        newest = max(walked, key=lambda c: c.at).id if walked else None
        on_staging = None if staging is None else found.at[staging] <= place
        waiting.append({"task": merge.task, "check": newest, "on_staging": on_staging})
    return {"waiting": waiting, "staging": staging, "cap": cap}


def next_turn(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    """`factory turn next`; while the cap of merges waits, only for a fix of a scenario their check failed."""
    cap = ctx.settings().get_int("rounds.bundle_merges")
    found = line(ctx) if sum(t.status == turn.MERGED for t in Queue.of(ctx).tasks()[0]) >= cap else None
    if found is None or len(found.waiting) < cap:
        return turn.next_turn(ctx, args, data)
    failing = _failing(found, Queue.of(ctx).checks()[0], cap)
    held = f"{len(found.waiting)} merges wait for their check, the cap of {cap}"
    if not failing:
        raise Refused("bundle-full", f"{held}: check their bundle first (factory queue bundle)")
    default = ctx.settings().get_str("repo.default_branch")

    def fixes(task: Task) -> bool:
        changed = turn.git(ctx, "diff", "--name-only", f"refs/heads/{default}...refs/heads/{task.branch}").split()
        return any(scenarios.covers(protects, path) for protects in failing.values() for path in changed)

    names = ", ".join(sorted(failing))
    return turn.give(ctx, fixes, f"{held}: only a fix of what their check failed ({names}) takes a turn, and no "
                                 "qa-clean task changes a path those protect")  # fmt: skip


def _failing(found: Line, checks: list[CheckRecord], cap: int) -> dict[str, tuple[str, ...]]:
    """The standing scenarios whose newest row, walked with the first ``cap`` merges in, failed: what each protects."""
    newest: dict[str, str] = {}
    since = found.at[found.waiting[cap - 1].commit]
    for check in sorted(checks, key=lambda c: c.at):
        if check.commit in found.at and found.at[check.commit] <= since:
            newest |= {row.scenario: row.result for row in check.rows}
    return {name: found.standing[name].protects for name, result in newest.items()
            if result not in (PASS, BLOCKED) and name in found.standing}  # fmt: skip
