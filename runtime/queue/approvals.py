"""The owner's approvals, bound to what was approved (R20).

An approval records a hash of the task's title, rung, model and picture. A picture's name carries its content
hash, so a new picture is a new name. Any later change to one of them voids the approval and the card returns.
The records live in the warden's state, where no agent can write; the permit reads them through ``current`` and
``matches``.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from pathlib import Path

from runtime.shared import models, store
from runtime.shared.models import Approval, Task
from runtime.shared.paths import Places


def digest(task: Task, model: str) -> str:
    bound = json.dumps([task.id, task.title, task.run, model, task.picture])
    return hashlib.sha256(bound.encode()).hexdigest()


def _path(places: Places, task_id: str) -> Path:
    return places.approvals / f"{task_id}.json"


def _go_path(places: Places, task_id: str) -> Path:
    return places.approvals / f"{task_id}.go.json"


def current(places: Places, task_id: str) -> Approval | None:
    """The task's approval, or None; a record that cannot be read raises."""
    raw = store.read_json_or(_path(places, task_id), None)
    return None if raw is None else models.load(Approval, raw)


def matches(approval: Approval, task: Task) -> bool:
    """Whether the approval still covers the task as it is now."""
    return (
        bool(approval.digest)
        and approval.task == task.id
        and approval.rung == task.run
        and approval.digest == digest(task, approval.model)
    )


def record(places: Places, task: Task, model: str, at: str, routed: int) -> Approval:
    """The owner's approval of ``task`` at its run on ``model``, with the rung its facts routed to then."""
    if task.run is None:
        raise ValueError(f"{task.id} has no run to approve")
    approval = Approval(task.id, task.run, model, "owner", at, digest(task, model), routed)
    store.write_json(_path(places, task.id), models.dump(approval), mode=0o600)
    return approval


def record_go(places: Places, task_id: str, at: str) -> None:
    """The owner's Go on a rung-4 spec, which the permit asks for before the build starts."""
    store.write_json(_go_path(places, task_id), {"task": task_id, "by": "owner", "at": at}, mode=0o600)


def has_go(places: Places, task_id: str) -> bool:
    return _go_path(places, task_id).exists()


def void(places: Places, task_id: str) -> None:
    for path in (_path(places, task_id), _go_path(places, task_id)):
        with suppress(FileNotFoundError):
            path.unlink()
