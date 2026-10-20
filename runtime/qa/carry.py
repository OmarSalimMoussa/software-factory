"""Carry-over: a scenario's full pass at an earlier walk stands for it in a later check of the default branch, so no
tester walks it again.

A pass at commit P carries to a check at Q when it is the scenario's newest walk at Q or before it on the branch's
first-parent line (the latest saved at the nearest commit), a full one (it passed, having run every step the
scenario has), and the diff from P to Q touches nothing the scenario depends on: a path its `protects` names, a path
the product lists in its shared-paths file as one every scenario depends on, that file, the scenario's own file, or
anything else in qa/ but another scenario's file. Its file and the shared-paths file must also be the same at the
tip, whose protects and steps the bundle reads. A scenario that names no protects never carries, and none does
while the product lists no shared paths, or lists them in a file that cannot be read: the bundle's answer says so.

A carried scenario is a row of the check's Carried table, which only the warden writes, naming the walk its pass
came from; only a walked row is a source, so a carry always names a real walk. A check with nothing left to walk
saves with no report: no tester starts, and no build of slot 0's is needed.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from runtime.qa import scenarios
from runtime.qa.scenarios import FOLDER, SHARED, Scenario
from runtime.queue import turn
from runtime.queue.book import Queue
from runtime.queue.records import Carried, CheckRecord
from runtime.shared.protocol import Refused
from runtime.warden.context import Context

PASS = "pass"
QA = str(PurePosixPath(FOLDER).parent)
OFF = "no earlier pass carries"


class Line(Protocol):
    @property
    def commits(self) -> list[str]: ...

    @property
    def at(self) -> dict[str, int]: ...

    @property
    def standing(self) -> dict[str, Scenario]: ...


@dataclass(frozen=True, slots=True)
class Carry:
    rows: dict[str, Carried]  # each scenario that carries, by name
    off: str  # why no pass carries, or ""


def carried(ctx: Context, found: Line, checks: list[CheckRecord], names: Iterable[str], walk_at: str) -> Carry:
    """Which of ``names`` carry an earlier full pass to a walk at ``walk_at``."""
    names, tip = list(names), found.commits[0]
    shared, off = _shared(ctx, tip) if names else ((), "")
    if off:
        return Carry({}, off)
    diffs: dict[tuple[str, str], list[str]] = {}
    rows = {}
    for name in names:
        source = _source(found, checks, name, walk_at)
        if source is None:
            continue
        since, to_tip = _diff(ctx, diffs, source.commit, walk_at), _diff(ctx, diffs, source.commit, tip)
        mine = (f"{FOLDER}/{name}.md", SHARED)
        if not any(_depends(found.standing[name], shared, p) for p in since) and not set(mine) & set(to_tip):
            rows[name] = source
    return Carry(rows, "")


def onto(ctx: Context, found: Line, record: CheckRecord, names: frozenset[str]) -> list[Carried]:
    """The rows of ``names`` a check lacks whose pass carries; with nothing walked, every one must carry."""
    left = sorted(names - {row.scenario for row in record.rows})
    rows = carried(ctx, found, Queue.of(ctx).checks()[0], left, record.commit).rows
    still = [name for name in left if name not in rows]
    if not record.rows and (still or not left):
        walk = f"walk {', '.join(still)} there" if still else "walk a scenario there"
        raise Refused(
            "check",
            f"{walk} and save the rows with --body <report>: a check saves without one only when every scenario "
            f"the merges up to {record.commit[:12]} protect carries an earlier full pass",
        )
    return [rows[name] for name in left if name in rows]


def _shared(ctx: Context, tip: str) -> tuple[tuple[str, ...], str]:
    """The paths every scenario shares, as the tip lists them, or why no pass carries."""
    if not turn.git(ctx, "ls-tree", "--name-only", tip, "--", SHARED):
        return (), f"{OFF}: the product names no paths its scenarios share in {SHARED}"
    paths, problems = scenarios.shared(turn.git(ctx, "show", f"{tip}:{SHARED}"))
    return paths, f"{OFF}: {SHARED}: {'; '.join(problems)}" if problems else ""


def _source(found: Line, checks: list[CheckRecord], name: str, walk_at: str) -> Carried | None:
    """The scenario's newest walk at ``walk_at`` or before it, when it is a full pass."""
    here, scenario = found.at[walk_at], found.standing[name]
    walks = [
        (-found.at[check.commit], check.at, check, row)
        for check in checks
        if check.commit in found.at and found.at[check.commit] >= here
        for row in check.rows
        if row.scenario == name
    ]
    if not walks or not scenario.protects:
        return None
    _, _, check, row = max(walks, key=lambda walk: walk[:2])
    full = row.result == PASS and row.steps >= len(scenario.steps)
    return Carried(name, check.id, check.commit) if full else None


def _diff(ctx: Context, diffs: dict[tuple[str, str], list[str]], since: str, until: str) -> list[str]:
    if since != until and (since, until) not in diffs:
        diffs[since, until] = turn.git(ctx, "diff", "--name-only", "--no-renames", since, until).splitlines()
    return diffs.get((since, until), [])


def _depends(scenario: Scenario, shared: tuple[str, ...], path: str) -> bool:
    other = scenarios.named(path) not in (None, scenario.name)
    return (
        path == SHARED
        or scenarios.covers((*scenario.protects, *shared), path)
        or (scenarios.covers((QA,), path) and not other)
    )
