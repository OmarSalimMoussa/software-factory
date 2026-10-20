"""What a push to prod ships: the newest green build of the default branch, and the merges that wait behind it.

The walk goes back along the first-parent line from the build staging (slot 0) answers on toward prod's build. It
takes the first commit staging answered on, by the deploy log, that keeps every build it answered with; whose tree
passed factory check; and whose merges since prod's build are all cleared: each one's task passed its check of the
default branch, or a revert up to that commit took it back. Prod gains the tasks merged up to it. Each merge after it
waits, with one reason: its own check, a held merge before it, factory check, or staging not having run it. When no
commit qualifies, the plan is a refusal that names the oldest merge held back. Before prod's first build, every merge
is cleared.

The snapshot runs no git, so the warden's done thread works the plan out and writes it: when staging's or prod's
build moves, and watch.done_minutes after the last time. A push works it out again under the push lock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runtime.queue.book import Queue
from runtime.queue.turn import merges
from runtime.shared import store
from runtime.shared.models import ON_STAGING, Probe
from runtime.shared.paths import Places
from runtime.shared.protocol import Refused
from runtime.warden import checks
from runtime.warden.context import Context
from runtime.warden.delivery import deploys
from runtime.warden.delivery.github_push import REVERTS, Git, fetch_checkout, pushing
from runtime.warden.health import probes

# A task whose merge passed its check of the default branch. A closed task's merge ships only on a saved check of
# that merge that passed: a task may be closed before its check.
SHIPPABLE = frozenset({"checked", ON_STAGING, "promoted"})
Line = list[tuple[str, str, bool]]  # first-parent commits, newest first: each one's sha, its tree, whether a merge


def path(places: Places) -> Path:
    return places.state / "push-plan.json"


def read(places: Places) -> dict[str, Any] | None:
    found: dict[str, Any] | None = store.read_json_or(path(places), None)
    return found


def slot0(ctx: Context) -> Probe:
    """Staging's last probe; refused while staging is red or unknown."""
    found = probes.recent(ctx.places, probes.STAGING, 1)
    if not found:
        raise Refused("staging-unknown", "staging has not been probed yet: wait a minute, then push")
    if not found[0].ok:
        raise Refused("staging-red", f"staging is red ({found[0].error}): fix staging before pushing it to prod")
    return found[0]


def _prod(places: Places) -> str | None:
    current = deploys.builds(places, probes.PROD)["current"]
    return None if current is None else current["commit"]


def refresh(ctx: Context) -> None:
    latest = probes.recent(ctx.places, probes.STAGING, 1)
    if _due(ctx, read(ctx.places), latest[0].commit if latest and latest[0].ok else None):
        with pushing:
            fresh(ctx)


def _due(ctx: Context, found: dict[str, Any] | None, staging: str | None) -> bool:
    if found is None or (found["staging"], found["prod"]) != (staging, _prod(ctx.places)):
        return True
    return ctx.clock.age(found["at"]) >= ctx.settings().get_int("watch.done_minutes") * 60


def fresh(ctx: Context) -> dict[str, Any]:
    """The plan from a fresh fetch of the agents' checkout, written for the push sheet. The caller holds pushing."""
    git = Git(ctx)
    tip = fetch_checkout(ctx, git)
    plan: dict[str, Any] = {"at": ctx.clock.iso(), "staging": None, "prod": _prod(ctx.places), "ships": None,
                            "gains": [], "held": [], "refused": None}  # fmt: skip
    try:
        staging = slot0(ctx)
    # telemetry: surfaced — the plan's `refused` carries it: the push sheet draws it, and the push refuses with it
    except Refused as exc:
        plan["refused"] = {"rule": exc.rule, "reason": exc.reason}
    else:
        plan["staging"] = staging.commit
        plan.update(_Walk(ctx, git, staging, plan["prod"]).plan(tip))
    store.write_json(path(ctx.places), plan)
    return plan


def on_branch(git: Git, sha: str, tip: str) -> str | None:
    """The full commit of ``sha`` when the default branch, whose tip is ``tip``, holds it."""
    return git.full(sha) if git.knows(sha) and git.is_ancestor(sha, tip) else None


def off_branch(ctx: Context, sha: str) -> Refused:
    branch = ctx.settings().get_str("repo.default_branch")
    return Refused("off-branch", f"{sha[:12]} is not on {branch} in the checkout: only {branch}'s work goes to prod")


def _reverted(git: Git, commits: str) -> set[str]:
    return set(REVERTS.findall(git.run("log", "--format=%B", commits)))


def unchecked_merges(ctx: Context, git: Git, base: str, head: str) -> list[str]:
    """The merges on the default branch after ``base`` up to ``head`` not cleared to ship; one reverted within that
    range ships nothing of its own."""
    clearance, reverted = Clearance(ctx), _reverted(git, f"{base}..{head}")
    found = git.run("rev-list", "--first-parent", "--merges", f"{base}..{head}").split()
    return [merge for merge in found if merge not in reverted and clearance.why(merge) is not None]


class Clearance:
    """Whether each merge on the default branch is cleared to ship, as its task's status and saved checks say."""

    def __init__(self, ctx: Context) -> None:
        self.tasks, self.queue = merges(ctx.places), Queue.of(ctx)
        self.passed = {c.commit for c in self.queue.checks()[0] if all(r.result == "pass" for r in c.rows)}
        self.statuses: dict[str, str] = {}

    def why(self, merge: str) -> str | None:
        task = self.tasks.get(merge)
        if task is None:
            return "no task's merge"
        if task not in self.statuses:
            self.statuses[task] = self.queue.task(task).status
        status = self.statuses[task]
        cleared = status in SHIPPABLE or (status == "closed" and merge in self.passed)
        return None if cleared else "its check has not passed"


class _Walk:
    def __init__(self, ctx: Context, git: Git, staging: Probe, prod: str | None) -> None:
        self.ctx, self.git, self.staging = ctx, git, staging
        self.base = None if prod is None else git.full(prod)
        self.clearance = Clearance(ctx)
        logged = {row["commit"]: row["at"] for row in deploys.log(ctx.places, probes.STAGING)}
        self.served = {staging.commit: staging.at} | logged

    def _since(self, head: str) -> str:
        return head if self.base is None else f"{self.base}..{head}"

    def _name(self, sha: str) -> str | None:
        return next((name for name in self.served if deploys.same_build(name, sha)), None)

    def _why(self, merge: str) -> str | None:
        return None if self.base is None else self.clearance.why(merge)

    def plan(self, tip: str) -> dict[str, Any]:
        head = on_branch(self.git, self.staging.commit, tip)
        if head is None:
            return _refused(off_branch(self.ctx, self.staging.commit))
        out = self.git.run("log", "--first-parent", "--format=%H %T %P", self._since(head))
        line: Line = [(row[0], row[1], len(row) > 3) for row in (text.split() for text in out.splitlines())]
        if not line:
            ran = f"{self.staging.commit[:12]}" + ("" if head == self.base else " and newer")
            return _refused(Refused("nothing-to-push", f"prod already runs {ran}"))
        at, reverted = self._newest_green(line)
        held = self._held(line, at, _reverted(self.git, self._since(head)))
        found = [self.clearance.tasks.get(m) for m, _, merge in reversed(line[at:]) if merge and m not in reverted]
        plan = {"gains": list(dict.fromkeys(task for task in found if task)), "held": [row for row, _ in held]}
        if at < len(line):
            name = self._name(line[at][0])
            assert name is not None  # the walk stops only at a build staging answered on
            return plan | {"ships": {"commit": name, "full": line[at][0], "at": self.served[name]}}
        if not held:
            branch = self.ctx.settings().get_str("repo.default_branch")
            said = f"no build staging ran since prod's passed factory check: run factory check on {branch}, then push"
            return plan | _refused(Refused("unchecked", said))
        first, rule = held[0]
        name = first["task"] or f"merge {first['merge'][:12]}"
        said = f"nothing since prod's build can ship yet: {name} is held back ({first['why']})"
        return plan | _refused(Refused(rule, said))

    def _newest_green(self, line: Line) -> tuple[int, set[str]]:
        """Where in ``line`` the newest commit that may ship is, past its end when none may; the merges reverted up
        to it."""
        for at, (sha, tree, _) in enumerate(line):
            if self._name(sha) is None or not checks.passed(self.ctx.places, tree):
                continue
            reverted = _reverted(self.git, self._since(sha))
            if all(self._why(m) is None for m, _, merge in line[at:] if merge and m not in reverted):
                return at, reverted
        return len(line), set()

    def _held(self, line: Line, at: int, reverted: set[str]) -> list[tuple[dict[str, Any], str]]:
        """Each merge newer than ``at`` that no revert took back, oldest first, with why it waits, and the rule a
        refusal names that by."""
        own = {k: self._why(line[k][0]) for k in range(at) if line[k][2] and line[k][0] not in reverted}
        out: list[tuple[dict[str, Any], str]] = []
        after, seen = None, set()
        for k in sorted(own, reverse=True):
            task = self.clearance.tasks.get(line[k][0])
            if own[k] is not None:
                rule, why = "uncleared", own[k]
            elif after is not None:
                rule, why = "held", f"comes after {after}, which is held"
            else:
                rule, why = self._waits(line, k, own)
            if own[k] is not None:
                after = task or f"merge {line[k][0][:12]}"
            if task is None or task not in seen:
                seen.add(task)
                out.append(({"task": task, "merge": line[k][0], "why": why}, rule))
        return out

    def _waits(self, line: Line, k: int, own: dict[int, str | None]) -> tuple[str, str]:
        """Why a cleared merge with none held before it waits: no commit from it up to the next held one both passed
        factory check and ran on staging."""
        stop = max((j for j, why in own.items() if why is not None and j < k), default=-1)
        if any(checks.passed(self.ctx.places, line[j][1]) for j in range(stop + 1, k + 1)):
            return "unserved", "staging has not run it yet"
        return "unchecked", "factory check has not passed on it yet"


def _refused(refusal: Refused) -> dict[str, Any]:
    return {"refused": {"rule": refusal.rule, "reason": refusal.reason}}
