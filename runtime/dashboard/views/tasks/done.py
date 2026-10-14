"""Work's Done tab: each finished task, newest merge first, with how far its merge has reached; or the default
branch's latest commits, each with its task; under the local branch's card, whose button opens slot 0 signed in."""

from __future__ import annotations

from typing import Any

from runtime.dashboard.views.kit import Query, Snap, ago, epoch, one_of, page, plural
from runtime.dashboard.views.words import taskwords

VIEWS = (("tasks", "Tasks"), ("commits", "Commits"))
_STAGES = (("github", "GitHub"), ("staging", "staging"), ("prod", "prod"))
_STATE = {True: ("good", "has it"), False: ("", "does not have it yet"), None: ("muted", "is not known yet")}
_NOTE = {"tasks": "newest merge first", "commits": "newest first"}


# A closed task whose merge stayed is done too.
_FINISHED = (*taskwords.LANDED, "closed")


def listed(snap: Snap) -> list[dict[str, Any]]:
    """The warden's done tasks whose status still says they landed: a revert moves a task back before the warden
    reads the branch again."""
    found = snap.get("done")
    status = {i: t["status"] for i, t in taskwords.tasks(snap).items()}
    return [] if found is None else [row for row in found["tasks"] if status.get(row["task"]) in _FINISHED]


def ids(snap: Snap) -> set[str]:
    """The tasks Done lists, which the Tasks tab leaves out."""
    return {row["task"] for row in listed(snap)}


def _stages(row: dict[str, Any]) -> list[dict[str, str]]:
    stages = [{"label": "local", "tone": "good", "title": "The local branch has it"}]
    for key, label in _STAGES:
        tone, words = _STATE[row[key]]
        stages.append({"label": label, "tone": tone, "title": f"{label[0].upper()}{label[1:]} {words}"})
    return stages


def _task_row(snap: Snap, row: dict[str, Any], title: str) -> dict[str, Any]:
    return {
        "id": row["task"],
        "title": title,
        "sub": f"merged {ago(row['at'], snap.now)} · {plural(len(row['commits']), 'commit')}",
        "stages": _stages(row),
    }


def _commit_row(snap: Snap, commit: dict[str, Any]) -> dict[str, Any]:
    place = [commit["task"], f"slice {commit['slice']}" if commit["slice"] else None] if commit["task"] else []
    return {
        "sha": commit["sha"][:7],
        "subject": commit["subject"],
        "task": commit["task"],
        "note": " · ".join(p for p in place if p) or None,
        "age": ago(commit["at"], snap.now),
    }


def _behind(slot: dict[str, Any]) -> str:
    if slot["behind"] is None:
        return "not known how far behind"
    return "up to date" if slot["behind"] == 0 else f"{plural(slot['behind'], 'commit')} behind"


def _button(snap: Snap, slot: dict[str, Any]) -> dict[str, Any]:
    settings = snap.settings
    address = settings.get_str("slots.owner_address")
    if slot["commit"] is None:
        return {"label": "Slot 0 is down", "url": None, "title": slot["why"]}
    label = f"Open {settings.get_str('repo.github').split('/')[-1]}"
    if not address:
        return {"label": label, "url": None, "title": "Set the test address you sign in as under Settings, Slots."}
    return {"label": label, "url": "/api/slot/open", "title": f"Opens slot 0 on this laptop, signed in as {address}"}


def _master(snap: Snap, found: dict[str, Any]) -> dict[str, Any]:
    ahead, slot = found["ahead"], found["slot0"]
    github = (
        "GitHub's copy is not read yet"
        if ahead is None
        else "GitHub has all of it"
        if ahead == 0
        else f"{plural(ahead, 'commit')} GitHub does not have"
    )
    runs = "Slot 0 gives no commit" if slot["commit"] is None else f"Slot 0 runs {slot['commit'][:7]}, {_behind(slot)}"
    return {
        "label": f"Local {snap.settings.get_str('repo.default_branch')}",
        "sha": found["local"][:7],
        "github": github,
        "slot": f"{runs} · checked {ago(epoch(found['at']), snap.now)}",
        "button": _button(snap, slot),
    }


def done(snap: Snap, query: Query) -> dict[str, Any]:
    view = one_of(query.get("view", "tasks"), [k for k, _ in VIEWS], "view")
    found = snap.get("done")
    base = {"views": [list(v) for v in VIEWS], "view": view, "note": _NOTE[view]}
    if found is None:
        return {**base, "master": None, "rows": [], "more": False, "empty": "The warden has not read what is done yet."}
    if view == "tasks":
        tasks = taskwords.tasks(snap)
        rows = [_task_row(snap, row, tasks[row["task"]]["title"]) for row in listed(snap)]
        empty = "No task is done yet."
    else:
        rows = [_commit_row(snap, c) for c in found["commits"]]
        empty = "The default branch has no commits yet."
    shown, more = page(rows, query, snap.settings.get_int("dashboard.page_size"))
    return {**base, "master": _master(snap, found), "rows": shown, "more": more, "empty": empty}
