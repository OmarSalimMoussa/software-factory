"""Merge-check bundles: the merges that wait for their check of the default branch, checked together.

A merge waits from when the warden finds it until a check clears it, its task at merged; a task merged twice waits by
its newest merge. A bundle is the oldest waiting merges, at most rounds.bundle_merges, walked at one commit: the one
just before the next merge after the bundle's last, or the tip, so a revert after its last merge is in it. Its
scenarios are the standing ones whose `protects` names a path one of its merges changed, read at the tip, where the
testers read them.

A saved check of a commit on the default branch's first-parent line clears every merge waiting up to it, at once, when
every row passed, each scenario those merges protect has a row, slot 0's last probe named that commit, and they are
at most rounds.bundle_merges, or the check of the first that many failed. Anything less clears none, and each row that
did not pass says what comes next (`_next`). No merge turn goes while the cap of merges waits, until the check of their
bundle failed: then a fix may land, and join the bundle past the cap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from runtime.qa import scenarios
from runtime.queue import statuses, tasks, turn
from runtime.queue.book import Queue
from runtime.queue.records import CheckRecord, ScenarioRow
from runtime.shared.protocol import Refused, ok
from runtime.warden.context import Context
from runtime.warden.delivery import deploys
from runtime.warden.health import probes

PASS, BLOCKED = "pass", "blocked"
_MERGE = re.compile(r"^merge ([0-9a-f]{40})$")  # the line `git log --format='merge %H'` opens each merge with


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
    standing: list[str]  # the standing scenarios a tester may walk now

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
    standing = _standing(ctx, commits[0], settings.get_int("qa.max_steps"))
    changed = _changed(ctx, [newest[task] for task in order])
    waiting = []
    for task in order:
        commit = newest[task]
        later = [n for n in merge_places if n < at[commit]]
        guards = {name for name, protects in standing.items() for path in changed[commit] if _covers(protects, path)}
        waiting.append(Merge(commit, task, commits[max(later) + 1] if later else commits[0], frozenset(guards)))
    return Line(commits, at, waiting, sorted(standing))


def _covers(protects: tuple[str, ...], path: str) -> bool:
    return any(path == entry.rstrip("/") or path.startswith(entry.rstrip("/") + "/") for entry in protects)


def _standing(ctx: Context, tip: str, max_steps: int) -> dict[str, tuple[str, ...]]:
    """Each scenario at ``tip`` a tester may walk now (none waits for what it needs), by name: what it protects."""
    repo = ctx.settings().get_str("repo.path")
    argv = ["git", "-C", repo, "grep", "-n", "--full-name", "-I", "-e", "", tip, "--", f"{scenarios.FOLDER}/*.md"]
    code, out = ctx.launcher.agent_command(argv, repo)
    if code == 1 and not out.strip():  # git grep found no line: the product has no scenario yet
        return {}
    if code != 0:
        raise Refused("git", f"git grep failed: {out.strip()[-200:]}")
    texts: dict[str, list[str]] = {}
    for row in out.splitlines():
        rel, _, numbered = row.removeprefix(f"{tip}:").partition(":")
        texts.setdefault(rel, []).append(numbered.partition(":")[2])
    found, problems = {}, []
    for rel, lines in texts.items():
        name = rel.removeprefix(f"{scenarios.FOLDER}/").removesuffix(".md")
        if "/" in name or name == "README":
            continue
        scenario, said = scenarios.parse(name, "\n".join(lines) + "\n", max_steps)
        problems += said
        if scenario is not None and scenario.runs != "waiting":
            found[name] = scenario.protects
    if problems:
        raise Refused("scenarios", "; ".join(problems))
    return found


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
        return {"cleared": [], "next": [_next(found, checks, record, row) for row in failing]}
    first = _past_cap(ctx, found, checks, members)
    if first:
        cap = ctx.settings().get_int("rounds.bundle_merges")
        why = f"{len(members)} merges wait up to {record.commit[:12]}, past the cap of {cap}: check the oldest {cap} "
        return {"cleared": [], "why": why + f"at {first[:12]} first (factory queue bundle)"}
    missing = sorted(frozenset[str]().union(*(m.scenarios for m in members)) - {row.scenario for row in record.rows})
    if missing:
        return {
            "cleared": [],
            "why": f"no row for {', '.join(missing)}, which the merges up to {record.commit[:12]} "
            "protect: walk them there and save the check again",
        }
    if members and not _slot0_ran(ctx, record.commit):
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


def _next(found: Line, checks: list[CheckRecord], record: CheckRecord, row: ScenarioRow) -> dict[str, Any]:
    """What a row that did not pass asks for: walk it again or alone, revert one merge, or wait for a fix."""
    said = {"scenario": row.scenario, "result": row.result}
    members = found.upto(record.commit)
    if row.result == BLOCKED:
        return {**said, "do": "again", "why": "walk it again once what blocked it is fixed: a block is never a revert"}
    if row.scenario not in found.standing:
        return {**said, "do": "read", "why": "it is no standing scenario, so no merge's protects names it"}
    if not members:
        return {**said, "do": "request", "why": "no merge waits up to this commit: file what failed as a request"}
    here, floor = found.at[record.commit], found.at[found.base]
    walks = [
        (found.at[c.commit], r.result)
        for c in checks
        if c.id != record.id and c.commit in found.at and found.at[c.commit] >= here
        for r in c.rows
        if r.scenario == row.scenario
    ]
    passes = [at for at, result in walks if result == PASS]
    if passes and min(passes) <= floor:
        return {**said, **_since_pass(found, members, row.scenario, min(passes), here)}
    if any(at == floor and result not in (PASS, BLOCKED) for at, result in walks):
        return {
            **said,
            "do": "wait",
            "why": f"it fails at {found.base[:12]} too, before every merge waiting: its fix "
            "takes the next merge turn and joins the bundle",
        }
    return {
        **said,
        "do": "walk",
        "at": found.base,
        "why": "it has not passed since before the merges waiting: walk it alone at their base first",
    }


def _since_pass(found: Line, members: list[Merge], scenario: str, passed: int, here: int) -> dict[str, Any]:
    """Its suspects: the merges since its pass at the place ``passed``, those that protect it if any do."""
    since = [m for m in members if found.at[m.commit] < passed]
    if passed == here or not since:
        return {"do": "again", "why": "it passed with every merge up to here in it: walk it again"}
    suspects = [m for m in since if scenario in m.scenarios] or since
    if len(suspects) == 1:
        return {
            "do": "revert",
            "merge": suspects[0].commit,
            "task": suspects[0].task,
            "why": f"the one merge since its pass at {found.commits[passed][:12]} that could break it",
        }
    middle = suspects[(len(suspects) + 1) // 2 - 1]
    return {
        "do": "walk",
        "at": middle.walk_at,
        "suspects": [m.task for m in suspects],
        "why": "walk it alone there: a pass leaves the later suspects, a fail the earlier",
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
    if args.get("through"):
        walk_at = turn.full_commit(ctx, str(args["through"]))
        on_line(ctx, found, walk_at)
        members = found.upto(walk_at)
        if not members:
            oldest = found.waiting[0].commit[:12]
            raise Refused("bundle", f"no merge waits up to {walk_at[:12]}; the oldest waiting is {oldest}")
        first = _past_cap(ctx, found, checks, members)
        if first:
            raise Refused(
                "bundle",
                f"{len(members)} merges is past the cap of {cap}: a bundle grows past it only "
                f"once the check of the first {cap}, at {first[:12]}, failed",
            )
    names = sorted(frozenset[str]().union(*(m.scenarios for m in members)))
    answer: dict[str, Any] = {
        "merges": [{"commit": m.commit, "task": m.task} for m in members],
        "walk_at": walk_at,
        "base": found.base,
        "scenarios": [{"name": s, "merges": [m.task for m in members if s in m.scenarios]} for s in names],
        "waiting": len(found.waiting),
    }
    if not names:
        answer["walk_instead"] = _longest(found, checks)
    newest = _newest(checks, walk_at)
    if newest is not None:
        answer["check"] = newest.id
        answer["next"] = [_next(found, checks, newest, row) for row in newest.rows if row.result != PASS]
    up = "factory slot up" if walk_at == found.commits[0] else f"factory slot up --at {walk_at[:12]}"
    told = f"{len(members)} of {len(found.waiting)} waiting merges: {up}, walk "
    told += f"{len(names)} scenarios" if names else f"{answer['walk_instead']}, as no scenario protects them"
    return ok(**answer, message=told + f", then factory queue check {walk_at[:12]}")


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
    """`factory turn next`, unless the cap of merges waits and the check of their bundle has not failed."""
    cap = ctx.settings().get_int("rounds.bundle_merges")
    if sum(t.status == turn.MERGED for t in Queue.of(ctx).tasks()[0]) >= cap:
        waiting = line(ctx).waiting
        if len(waiting) >= cap and not _failed(Queue.of(ctx).checks()[0], waiting[cap - 1].walk_at):
            raise Refused(
                "bundle-full",
                f"{len(waiting)} merges wait for their check, the cap of {cap}: check their "
                "bundle first (factory queue bundle)",
            )
    return turn.next_turn(ctx, args, data)
