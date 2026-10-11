"""The queue's documents beside requests and tasks: a designer's draft spec, a saved check of the default branch,
and a task's log (one JSON object a line). Each parses into one form and dumps the one form a parse accepts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from runtime.queue import formats
from runtime.queue.formats import (
    ARTIFACT,
    CHECK_KEYS,
    CHECK_RESULTS,
    CHECK_TABLE,
    GOAL_ITEM,
    ISO,
    LOG_KEYS,
    LOG_OPTIONAL,
    SHA,
    SPEC_KEYS,
    SPEC_STATUSES,
    TASK_ID,
    FormatError,
)


@dataclass(slots=True)
class Spec:
    """A designer's draft spec: the short version the Library shows, and the text it saved."""

    task: str
    goal_item: str
    rung: int | None
    status: str
    brief: str
    updated: str
    title: str
    body: str


@dataclass(slots=True)
class ScenarioRow:
    scenario: str
    result: str
    steps: int
    evidence: str


@dataclass(slots=True)
class CheckRecord:
    """A saved check of the default branch: what each standing scenario did on one commit."""

    id: str
    commit: str
    at: str
    title: str
    rows: list[ScenarioRow] = field(default_factory=list)
    notes: str = ""


def parse_spec(text: str, rel: str = "") -> Spec:
    meta, rest, problems = formats.split_front(text, SPEC_KEYS)
    if problems:
        formats.raise_all(problems, rel)
    title, body = formats.titled(rest, problems)
    spec = Spec(
        meta["task"],
        meta["goal_item"],
        formats.opt_int(meta["rung"], "rung", problems),
        meta["status"],
        meta["brief"],
        meta["updated"],
        title,
        body,
    )
    if not TASK_ID.match(spec.task):
        problems.append(f"task {spec.task!r} is not a task id")
    if spec.status not in SPEC_STATUSES:
        problems.append(f"status {spec.status!r} is not one of {', '.join(SPEC_STATUSES)}")
    if spec.brief and not spec.brief.startswith(ARTIFACT):
        problems.append(f"brief must be a {ARTIFACT} artifact link")
    if spec.goal_item and not GOAL_ITEM.match(spec.goal_item):
        problems.append(f"goal_item {spec.goal_item!r} is not a goal item's name, such as G-build-sop")
    if not ISO.match(spec.updated):
        problems.append(f"updated {spec.updated!r} is not a UTC time")
    if not body:
        problems.append("the spec has no text under its title")
    formats.check_name(rel, spec.task, problems)
    formats.raise_all(problems, rel)
    return spec


def dump_spec(spec: Spec) -> str:
    values = {
        "task": spec.task,
        "goal_item": spec.goal_item,
        "rung": "" if spec.rung is None else str(spec.rung),
        "status": spec.status,
        "brief": spec.brief,
        "updated": spec.updated,
    }
    return formats.render(SPEC_KEYS, values, f"# {spec.title}\n\n{spec.body}")


def parse_check(text: str, rel: str = "") -> CheckRecord:
    meta, rest, problems = formats.split_front(text, CHECK_KEYS)
    if problems:
        formats.raise_all(problems, rel)
    title, body = formats.titled(rest, problems)
    record = CheckRecord(meta["id"], meta["commit"], meta["at"], title)
    if not SHA.match(record.commit):
        problems.append(f"commit {record.commit!r} is not a commit sha")
    if not ISO.match(record.at):
        problems.append(f"at {record.at!r} is not a UTC time")
    record.rows, record.notes = _scenario_table(body, problems)
    formats.check_name(rel, record.id, problems)
    formats.raise_all(problems, rel)
    return record


def _scenario_table(body: str, problems: list[str]) -> tuple[list[ScenarioRow], str]:
    lines = body.split("\n")
    if "## Scenarios" not in lines:
        problems.append("no ## Scenarios table")
        return [], body
    heading = lines.index("## Scenarios")
    start = heading + 1
    while start < len(lines) and not lines[start].strip():
        start += 1
    head = lines[start] if start < len(lines) else ""
    rule = lines[start + 1] if start + 1 < len(lines) else ""
    if head != CHECK_TABLE or not rule.startswith("|") or set(rule) - set("|- "):
        problems.append(f"the Scenarios table starts with {CHECK_TABLE} and its --- row")
        return [], body
    rows: list[ScenarioRow] = []
    end = start + 2
    while end < len(lines) and lines[end].startswith("|"):
        cells = [c.strip() for c in lines[end].strip().strip("|").split("|")]
        if len(cells) != 4 or not cells[0] or cells[1] not in CHECK_RESULTS or not cells[2].isdigit():
            problems.append(f"scenario row {lines[end]!r} is not name | pass, fail or blocked | steps | evidence")
        else:
            rows.append(ScenarioRow(cells[0], cells[1], int(cells[2]), cells[3]))
        end += 1
    if not rows:
        problems.append("the Scenarios table has no rows")
    return rows, "\n".join(lines[:heading] + lines[end:]).strip()


def dump_check(record: CheckRecord) -> str:
    table = "\n".join(f"| {r.scenario} | {r.result} | {r.steps} | {r.evidence} |" for r in record.rows)
    sep = "| --- | --- | --- | --- |"
    body = f"# {record.title}\n\n## Scenarios\n\n{CHECK_TABLE}\n{sep}\n{table}\n\n{record.notes}"
    return formats.render(CHECK_KEYS, {"id": record.id, "commit": record.commit, "at": record.at}, body)


def dump_log(entry: dict[str, Any]) -> bytes:
    problems = log_problems(entry)
    if problems:
        raise FormatError(problems)
    return (json.dumps(entry, sort_keys=True) + "\n").encode()


def log_problems(entry: object) -> list[str]:
    if not isinstance(entry, dict):
        return ["a log line is a JSON object"]
    missing, unknown = LOG_KEYS - entry.keys(), entry.keys() - LOG_KEYS - LOG_OPTIONAL
    problems = [f"a log line is missing {', '.join(sorted(missing))}"] if missing else []
    if unknown:
        problems.append(f"a log line has unknown {', '.join(sorted(unknown))}")
    if any(not isinstance(v, str) or "\n" in v for v in entry.values()):
        problems.append("every log value is one line of text")
    if isinstance(entry.get("at"), str) and not ISO.match(entry["at"]):
        problems.append("a log line's at is not a UTC time")
    return problems


def parse_log(text: str, rel: str = "") -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    problems: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        try:
            entry = json.loads(line)
        except ValueError:
            problems.append(f"line {n} is not JSON")
            continue
        found = log_problems(entry)
        problems += [f"line {n}: {p}" for p in found]
        if not found:
            entries.append(entry)
    formats.raise_all(problems, rel)
    return entries
