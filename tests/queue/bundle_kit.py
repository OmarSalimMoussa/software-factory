"""The world's checkout as a git repo with standing scenarios: merges recorded for tasks, slot 0's probes, and the
checks the orchestrator saves of it."""

from __future__ import annotations

import dataclasses
from typing import Any

from product_git import ProductGit, run
from queue_kit import World

from runtime.queue import records, turn, verbs
from runtime.shared import models, store
from runtime.shared.models import Probe

SCENARIOS = {
    "web": "protects: web/\nneeds: nothing\nruns: in-turn",
    "api": "protects: api/handlers.py\nneeds: nothing\nruns: in-turn",
    "later": "protects: web/\nneeds: seed none-yet\nruns: waiting",
}


class Product(ProductGit):
    """The world's checkout as a git repo: merges recorded for tasks, and the checks the orchestrator saves of it."""

    def __init__(self, world: World) -> None:
        super().__init__(world.repo, SCENARIOS)
        self.world = world
        world.launcher.git = run

    def merge(self, rel: str, status: str = "merged", *, text: str | None = None) -> tuple[str, str]:
        """A merge recorded as its task's, at ``status``: the merge and the task."""
        commit, branch = super().merge(rel, text=text)
        task = self.world.task(branch=branch)
        self.world.queue().write_task(dataclasses.replace(task, status=status, run=1))
        return self.record(commit, task.id), task.id

    def record(self, commit: str, task: str) -> str:
        found = turn.merges(self.world.places)
        store.write_json(turn.merges_path(self.world.places), {**found, commit: task})
        return commit

    def slot0(self, commit: str) -> None:
        """Slot 0's probe, naming the build it runs as its health answer does: by the short commit."""
        probe = Probe(self.world.clock.iso(), "staging", True, commit[:8], 40)
        store.append_jsonl(self.world.places.probes, models.dump(probe))

    def check(self, commit: str, steps: int = 1, **rows: str) -> dict[str, Any]:
        table = "".join(f"| {name} | {result} | {steps} | shot.png |\n" for name, result in rows.items())
        report = f"# Check\n\n## Scenarios\n\n{records.CHECK_TABLE}\n| --- | --- | --- | --- |\n{table}"
        return self.save(commit, report.encode())

    def save(self, commit: str, report: bytes | None) -> dict[str, Any]:
        self.world.clock.at += 60
        return verbs.check_save(self.world.ctx(), {"commit": commit}, report)

    def status(self, task: str) -> str:
        return self.world.queue().task(task).status
