"""The logs the dashboard draws, folded as they grow: retirements for History, cost by day and in total, each
agent's time, each environment's health streak and the guard's refusals. Each poll reads only the lines added since
the last one; one fold of each log is kept for the warden's life.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from runtime.shared import models, store
from runtime.shared.models import RETIRED


class Fold:
    """A JSON-lines log folded as it grows: each new whole line is stepped once; a replaced log starts over."""

    def __init__(self) -> None:
        self.inode, self.offset, self.lines = -1, 0, 0
        self.reset()

    def reset(self) -> None:
        raise NotImplementedError

    def step(self, raw: dict[str, Any]) -> None:
        raise NotImplementedError

    def unreadable(self, path: Path, exc: Exception) -> None:
        """A line that cannot be read fails the whole fold, unless the fold draws it as a row of its own."""
        raise store.StoreError(f"{path}: line {self.lines} cannot be read") from exc

    def read(self, path: Path) -> None:
        try:
            info = path.stat()
        # telemetry: expected — nothing was logged yet
        except FileNotFoundError:
            return
        if info.st_ino != self.inode or info.st_size < self.offset:
            self.inode, self.offset, self.lines = info.st_ino, 0, 0
            self.reset()
        with path.open("rb") as fh:
            fh.seek(self.offset)
            data = fh.read(info.st_size - self.offset)
        whole = data[: data.rfind(b"\n") + 1]
        for line in whole.splitlines():
            self.lines += 1
            if line.strip():
                try:
                    self.step(json.loads(line))
                # telemetry: surfaced — an unreadable line goes to `unreadable`: it fails the fold, or draws as a row
                except (ValueError, KeyError, TypeError) as exc:
                    self.unreadable(path, exc)
        self.offset += len(whole)


class History(Fold):
    """Every retirement history.jsonl records, oldest first, under the session id it ended with; a line that cannot
    be read is kept as a row that says so, so one bad line never hides the rest of History."""

    def reset(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.bound: dict[str, str] = {}

    def step(self, raw: dict[str, Any]) -> None:
        if "was" in raw:
            self.bound[str(raw["was"])] = str(raw["session"])
        elif raw["state"] == RETIRED:
            fields = ("at", "session", "name", "retired", "by", "why")
            self.rows.append({"line": self.lines, **{k: str(raw[k]) for k in fields}})

    def unreadable(self, path: Path, exc: Exception) -> None:
        self.rows.append({"line": self.lines, "error": f"line {self.lines} of {path.name} cannot be read"})

    def answer(self) -> list[dict[str, Any]]:
        return [{**row, "session": self.bound.get(row["session"], row["session"])} if "session" in row else row
                for row in self.rows]  # fmt: skip


class Cost(Fold):
    """Every call's cost by day and family, agent and model, and in total by agent, task and model. Each total is
    [dollars, calls with unknown price]: an unknown price is counted, never added as zero. Beside them, the same
    slices' tokens: [calls, input, output, cache read, cache written], and each task's in total."""

    def reset(self) -> None:
        self.days: dict[str, dict[str, dict[str, list[float]]]] = {}
        self.totals: dict[str, dict[str, list[float]]] = {"agent": {}, "task": {}, "model": {}}
        self.tokens: dict[str, dict[str, dict[str, list[int]]]] = {}
        self.task_tokens: dict[str, list[int]] = {}

    def step(self, raw: dict[str, Any]) -> None:
        record = models.load(models.CostRecord, raw)
        agent = record.subagent or record.session
        day = self.days.setdefault(record.at[:10], {"family": {}, "agent": {}, "model": {}})
        cells = [
            day["family"].setdefault(record.family, [0.0, 0]),
            day["agent"].setdefault(agent, [0.0, 0]),
            day["model"].setdefault(record.model, [0.0, 0]),
            self.totals["agent"].setdefault(agent, [0.0, 0]),
            self.totals["task"].setdefault(record.task, [0.0, 0]),
            self.totals["model"].setdefault(record.model, [0.0, 0]),
        ]
        for cell in cells:
            cell[0 if record.usd is not None else 1] += record.usd if record.usd is not None else 1
        counts = (1, record.input, record.output, record.cache_read, record.cache_write)
        used = self.tokens.setdefault(record.at[:10], {"family": {}, "agent": {}, "model": {}})
        for row in (
            used["family"].setdefault(record.family, [0] * 5),
            used["agent"].setdefault(agent, [0] * 5),
            used["model"].setdefault(record.model, [0] * 5),
            self.task_tokens.setdefault(record.task, [0] * 5),
        ):
            for n, count in enumerate(counts):
                row[n] += count  # fmt: skip

    def answer(self, days: int) -> dict[str, Any]:
        recent = sorted(self.days)[-days:]
        return {"days": {d: self.days[d] for d in recent}, "totals": self.totals,
                "tokens": {"days": {d: self.tokens[d] for d in recent}, "task": self.task_tokens}}  # fmt: skip


class Times(Fold):
    """Each agent's time in total: its parts, its task and role, whether it is a lead (a session of its own) or a
    subagent, and whether every transcript it was measured from was read whole."""

    def reset(self) -> None:
        self.agents: dict[str, dict[str, Any]] = {}

    def step(self, raw: dict[str, Any]) -> None:
        record = models.load(models.TimeRecord, raw)
        agent = self.agents.setdefault(record.subagent or record.session, {
            "task": record.task, "role": record.role, "lead": not record.subagent, "model": 0.0, "tools": {},
            "waiting": 0.0, "idle": 0.0, "whole": True,
        })  # fmt: skip
        for part in ("model", "waiting", "idle"):
            agent[part] += getattr(record, part)
        for key, seconds in record.tools.items():
            agent["tools"][key] = agent["tools"].get(key, 0.0) + seconds
        agent["whole"] = agent["whole"] and record.whole


class Streaks(Fold):
    """Since when each environment is healthy, or down by a counted failure; an uncounted failure changes nothing."""

    def reset(self) -> None:
        self.state: dict[str, dict[str, Any]] = {}

    def step(self, raw: dict[str, Any]) -> None:
        probe = models.load(models.Probe, raw)
        if (probe.ok or probe.counts) and self.state.get(probe.env, {}).get("ok") is not probe.ok:
            self.state[probe.env] = {"ok": probe.ok, "since": probe.at}


class Refusals(Fold):
    """The guard's refusals, by day and rule."""

    def reset(self) -> None:
        self.days: dict[str, dict[str, int]] = {}

    def step(self, raw: dict[str, Any]) -> None:
        day = self.days.setdefault(str(raw["at"])[:10], {})
        day[str(raw["rule"])] = day.get(str(raw["rule"]), 0) + 1


_folds: dict[tuple[str, str], Any] = {}


_folds_lock = threading.Lock()


def kept(kind: type[Any], path: Path) -> Any:
    with _folds_lock:
        return _folds.setdefault((kind.__name__, str(path)), kind())
