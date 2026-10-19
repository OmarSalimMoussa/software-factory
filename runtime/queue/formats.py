"""The queue's file formats (intake spec, "Files"): requests and tasks here, and the front matter every queue
document shares; specs, saved checks and task logs are in records.py.

A queue document is Markdown with ``key: value`` front matter between ``---`` lines. Every key is always written,
in a fixed order, and a file with a missing or unknown key is malformed: nothing is filled in. ``parse_*`` checks
every value and raises one FormatError listing every problem; ``dump_*`` writes the one form a parse accepts.
Cross-file rules (a task's requests point back at it) live in format_check.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from runtime.queue import goal
from runtime.shared.models import (
    CHAT,
    CHAT_ID,
    INBOXES,
    REQUEST_SEVERITIES,
    SURFACES,
    TASK_SEVERITIES,
    TASK_STATUSES,
    Request,
    Task,
)


class FormatError(ValueError):
    """A queue file is malformed; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


REQUEST_ID = re.compile(r"^r-\d{4}-\d{2}-\d{2}-\d{4}$")
TASK_ID = re.compile(r"^t-\d{4,}$")
# A saved check of the default branch: when it was saved, then its commit's first 7 characters (documents.py).
CHECK_ID = re.compile(r"^c-\d{8}T\d{6}Z-[0-9a-f]{7}$")
GOAL_ITEM = re.compile(rf"^{goal.NAME}$")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ROLE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
USER = re.compile(r"^user-[A-Za-z0-9-]{1,64}$")
# A tagged message's writer in the chat, by its id there: a person, or another agent's bot.
CHAT_FROM = "discord-"
CHAT_SENDER = re.compile(rf"^{CHAT_FROM}{CHAT_ID.pattern}$")
# The inboxes whose requests quote words from outside, kept under the id their report names.
QUOTED = ("users", CHAT)
OUTCOME = re.compile(r"^(?:(task|joined) (t-\d{4,})|(noise|parked) (\S.*))$")
PICTURE = re.compile(r"^pictures/[a-z0-9-]+\.(svg|png)$")
SHA = re.compile(r"^[0-9a-f]{7,40}$")
# The version of its task's brief a spec was signed off on (runtime/warden/owner/briefs.py), such as v3.
BRIEF = re.compile(r"^v[1-9][0-9]*$")
# What routing reads from the change itself (routing spec, "The dispatch ladder"). Only the last two need the
# owner's second sign-off; the others push the run to a designer.
OWNER_CLASSES = ("none", "migration", "new-service", "design", "large-visible", "large-backend")
SIGN_OFF_CLASSES = ("large-visible", "large-backend")
SPEC_STATUSES = ("draft", "review", "approved")
CHECK_RESULTS = ("pass", "fail", "blocked")
RUNGS = range(0, 5)

REQUEST_KEYS = ("id", "inbox", "from", "at", "severity", "where", "report", "outcome")
TASK_KEYS = (
    "id",
    "requests",
    "goal_item",
    "severity",
    "surfaces",
    "cause",
    "slices",
    "owner_class",
    "score",
    "status",
    "run",
    "why",
    "picture",
    "after",
    "boosted_at",
    "not_now",
    "branch",
    "created",
)
SPEC_KEYS = ("task", "goal_item", "rung", "status", "brief", "updated")
CHECK_KEYS = ("id", "commit", "at")
LOG_KEYS = frozenset({"at", "actor", "event", "text"})
LOG_OPTIONAL = frozenset({"from", "to", "commit"})
CHECK_TABLE = "| scenario | result | steps run | evidence |"
_FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


def epoch(iso: str) -> float:
    """Seconds since the epoch for a UTC time written as 2026-10-09T14:02:11Z."""
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def one_line(text: str) -> str:
    return " ".join(text.split())


def split_front(text: str, keys: tuple[str, ...]) -> tuple[dict[str, str], str, list[str]]:
    match = _FRONT.match(text)
    if not match:
        return {}, "", ["no front matter between --- lines"]
    meta: dict[str, str] = {}
    problems: list[str] = []
    for line in match.group(1).split("\n"):
        key, sep, value = line.partition(":")
        if not sep or key != key.strip() or not key:
            problems.append(f"front matter line {line!r} is not key: value")
        elif key in meta:
            problems.append(f"{key} appears twice")
        else:
            meta[key] = value.strip()
    missing = [k for k in keys if k not in meta]
    unknown = [k for k in meta if k not in keys]
    if missing:
        problems.append(f"missing {', '.join(missing)}")
    if unknown:
        problems.append(f"unknown {', '.join(unknown)}")
    return meta, match.group(2), problems


def render(keys: tuple[str, ...], values: dict[str, str], tail: str) -> str:
    bad = [k for k in keys if "\n" in values[k] or "\r" in values[k]]
    if bad:
        raise FormatError([f"{k} must be one line" for k in bad])
    front = "\n".join(f"{k}: {values[k]}".rstrip() for k in keys)
    return f"---\n{front}\n---\n{tail.strip()}\n"


def titled(rest: str, problems: list[str]) -> tuple[str, str]:
    head, _, body = rest.lstrip("\n").partition("\n")
    if not head.startswith("# ") or not head[2:].strip():
        problems.append("no # title under the front matter")
        return "", rest.strip()
    return head[2:].strip(), body.strip()


def _items(value: str) -> list[str]:
    return [v.strip() for v in value.split(",")] if value.strip() else []


def opt_int(value: str, key: str, problems: list[str]) -> int | None:
    if not value:
        return None
    if not value.isdigit():
        problems.append(f"{key} {value!r} is not a whole number")
        return None
    return int(value)


def _opt_iso(value: str, key: str, problems: list[str]) -> None:
    if value and not ISO.match(value):
        problems.append(f"{key} {value!r} is not a UTC time such as 2026-10-09T14:02:11Z")


def check_name(rel: str, stem: str, problems: list[str]) -> None:
    if rel and rel.rsplit("/", 1)[-1] != f"{stem}.md":
        problems.append(f"the file is named {rel.rsplit('/', 1)[-1]}, not {stem}.md")


def raise_all(problems: list[str], rel: str) -> None:
    if problems:
        raise FormatError([f"{rel}: {p}" if rel else p for p in problems])


def parse_request(text: str, rel: str = "") -> Request:
    meta, rest, problems = split_front(text, REQUEST_KEYS)
    if problems:
        raise_all(problems, rel)
    req = Request(
        meta["id"],
        meta["inbox"],
        meta["from"],
        meta["at"],
        meta["severity"],
        meta["where"],
        rest.strip(),
        meta["report"],
        meta["outcome"],
    )
    problems += request_problems(req)
    if rel and rel.split("/")[:2] != ["requests", req.inbox]:
        problems.append(f"a request from the {req.inbox} inbox lives in requests/{req.inbox}/")
    check_name(rel, req.id, problems)
    raise_all(problems, rel)
    return req


def request_problems(req: Request) -> list[str]:
    problems = [] if REQUEST_ID.match(req.id) else [f"id {req.id!r} is not r-YYYY-MM-DD-NNNN"]
    if req.inbox not in INBOXES:
        problems.append(f"inbox {req.inbox!r} is not one of {', '.join(INBOXES)}")
    if not _may_file(req):
        problems.append(f"from {req.from_role!r} cannot file in the {req.inbox} inbox")
    if not ISO.match(req.at):
        problems.append(f"at {req.at!r} is not a UTC time")
    if req.severity not in REQUEST_SEVERITIES:
        problems.append(f"severity {req.severity!r} is not one of {', '.join(REQUEST_SEVERITIES)}")
    elif req.inbox in QUOTED and req.severity == "severe":
        problems.append(f"a request from the {req.inbox} inbox is never severe on arrival")
    if not req.where:
        problems.append("where is empty: name the screen, endpoint, file or host")
    if (req.inbox in QUOTED) != bool(req.report):
        problems.append(f"a request from the {' or '.join(QUOTED)} inbox names its report, and no other does")
    if req.outcome and not OUTCOME.match(req.outcome):
        problems.append(f"outcome {req.outcome!r} is not 'task t-N', 'joined t-N', 'noise <why>' or 'parked <why>'")
    if not req.body:
        problems.append("the body is empty")
    return problems


def _may_file(req: Request) -> bool:
    """Whether the request's sender may file in its inbox: the owner in the owner's, a user in the users', whoever
    wrote in the chat in the chat's, an agent's role in its own."""
    if req.inbox == "owner":
        return req.from_role == "owner"
    if req.inbox == "users":
        return bool(USER.match(req.from_role))
    if req.inbox == CHAT:
        return bool(CHAT_SENDER.match(req.from_role))
    return bool(ROLE.match(req.from_role)) and not req.from_role.startswith(("owner", "user", CHAT_FROM))


def dump_request(req: Request) -> str:
    values = {
        "id": req.id,
        "inbox": req.inbox,
        "from": req.from_role,
        "at": req.at,
        "severity": req.severity,
        "where": req.where,
        "report": req.report,
        "outcome": req.outcome,
    }
    return render(REQUEST_KEYS, values, req.body)


@dataclass(frozen=True, slots=True)
class Outcome:
    """A request's outcome as values: what triage made of it, the task it became or joined, and why."""

    kind: str  # task, joined, noise or parked; "waiting" while triage has not reached it
    task: str  # "" for noise, parked or waiting
    reason: str  # "" but for noise and parked


def outcome(text: str) -> Outcome:
    """The one reading of an outcome line ('task t-N', 'joined t-N', 'noise <why>', 'parked <why>', or empty)."""
    match = OUTCOME.match(text)
    if not text or match is None:
        return Outcome("waiting", "", "")
    if match.group(1):
        return Outcome(match.group(1), match.group(2), "")
    return Outcome(match.group(3), "", match.group(4))


def parse_task(text: str, rel: str = "") -> Task:
    meta, rest, problems = split_front(text, TASK_KEYS)
    if problems:
        raise_all(problems, rel)
    title, body = titled(rest, problems)
    task = Task(
        id=meta["id"],
        title=title,
        requests=_items(meta["requests"]),
        goal_item=meta["goal_item"],
        severity=meta["severity"],
        surfaces=_items(meta["surfaces"]),
        status=meta["status"],
        created=meta["created"],
        body=body,
        cause=meta["cause"],
        slices=opt_int(meta["slices"], "slices", problems),
        owner_class=meta["owner_class"],
        score=_score(meta["score"], problems),
        run=opt_int(meta["run"], "run", problems),
        why=meta["why"],
        picture=meta["picture"],
        after=_items(meta["after"]),
        boosted_at=meta["boosted_at"],
        not_now=_flag(meta["not_now"], problems),
        branch=meta["branch"],
    )
    problems += task_problems(task)
    check_name(rel, task.id, problems)
    raise_all(problems, rel)
    return task


def _score(value: str, problems: list[str]) -> float | None:
    if not value:
        return None
    if not re.match(r"^\d+(\.\d+)?$", value):
        problems.append(f"score {value!r} is not a number")
        return None
    return float(value)


def _flag(value: str, problems: list[str]) -> bool:
    if value not in ("yes", "no"):
        problems.append(f"not_now {value!r} is not yes or no")
    return value == "yes"


def task_problems(task: Task) -> list[str]:
    problems = [] if TASK_ID.match(task.id) else [f"id {task.id!r} is not t-NNNN"]
    if not task.title or "\n" in task.title:
        problems.append("the title must be one line")
    if not task.requests:
        problems.append("a task links at least one request")
    problems += [f"request {r!r} is not a request id" for r in task.requests if not REQUEST_ID.match(r)]
    problems += _fact_problems(task)
    if "\n" in task.why or "\n" in task.cause:
        problems.append("why and cause are one line each")
    if task.picture and not PICTURE.match(task.picture):
        problems.append(f"picture {task.picture!r} is not pictures/<name>.svg or .png")
    problems += [f"after {a!r} is not a task id" for a in task.after if not TASK_ID.match(a)]
    _opt_iso(task.boosted_at, "boosted_at", problems)
    if not ISO.match(task.created):
        problems.append(f"created {task.created!r} is not a UTC time")
    # Routing reads these facts, so a task is not routed without them (routing spec, "What enforces it").
    routed = task.status not in ("queued", "parked", "closed")
    if routed and (not task.surfaces or task.slices is None or task.run is None):
        problems.append(f"a {task.status} task has its surfaces, slices and run")
    if task.status == "proposed" and not (task.why and task.picture):
        problems.append("a proposed task carries its why and its picture")
    return problems


def _fact_problems(task: Task) -> list[str]:
    """What is wrong with the facts routing reads: severity, goal item, surfaces, status, owner class and rung."""
    problems = []
    if task.severity not in TASK_SEVERITIES:
        problems.append(f"severity {task.severity!r} is not one of {', '.join(TASK_SEVERITIES)}")
    if task.goal_item and not GOAL_ITEM.match(task.goal_item):
        problems.append(f"goal_item {task.goal_item!r} is not a goal item's name, such as G-build-sop")
    elif not task.goal_item and task.severity != "severe":
        problems.append("a task that is not severe names a goal item")
    problems += [f"surface {s!r} is not one of {', '.join(SURFACES)}" for s in task.surfaces if s not in SURFACES]
    if "none" in task.surfaces and len(task.surfaces) > 1:
        problems.append("surfaces lists none beside other surfaces")
    if task.status not in TASK_STATUSES:
        problems.append(f"status {task.status!r} is not a task status")
    if task.owner_class not in OWNER_CLASSES:
        problems.append(f"owner_class {task.owner_class!r} is not one of {', '.join(OWNER_CLASSES)}")
    if task.run is not None and task.run not in RUNGS:
        problems.append(f"run {task.run} is not a rung from 0 to 4")
    return problems


def dump_task(task: Task) -> str:
    values = {
        "id": task.id,
        "requests": ", ".join(task.requests),
        "goal_item": task.goal_item,
        "severity": task.severity,
        "surfaces": ", ".join(task.surfaces),
        "cause": task.cause,
        "slices": "" if task.slices is None else str(task.slices),
        "owner_class": task.owner_class,
        "score": "" if task.score is None else format(task.score, "g"),
        "status": task.status,
        "run": "" if task.run is None else str(task.run),
        "why": task.why,
        "picture": task.picture,
        "after": ", ".join(task.after),
        "boosted_at": task.boosted_at,
        "not_now": "yes" if task.not_now else "no",
        "branch": task.branch,
        "created": task.created,
    }
    return render(TASK_KEYS, values, f"# {task.title}\n\n{task.body}")
