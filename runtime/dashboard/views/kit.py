"""What every view shares: the snapshot it reads, the effective settings, and the few ways the dashboard writes a
number, a time, a model or a family. The words the owner reads are made here, never in the browser.

Every time is shown in UTC, so a page reads the same on every device.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NamedTuple

from runtime.shared.settings import Settings

API = 1  # the payload version the UI was built for; a newer warden makes the page say "reload"


class PartError(Exception):
    """One part of a page cannot be drawn; the message is one plain sentence the owner reads."""


@dataclass
class Snap:
    """One snapshot answer: each wanted name's raw state, or why it could not be read."""

    now: float
    state: dict[str, Any]
    errors: dict[str, str]
    _settings: Settings | None = field(default=None, repr=False)

    def get(self, name: str) -> Any:
        if name in self.errors:
            raise PartError(self.errors[name])
        if name not in self.state:
            raise PartError(f"The warden sent no {name}.")
        return self.state[name]

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            raw = self.get("settings")
            self._settings = Settings(raw["tree"], raw["defaults"], raw["overrides"])
        return self._settings


Query = Mapping[str, str]
Parts = dict[str, Callable[[], Any]]

FAMILIES = {
    "orch": ("Orchestrator", "--f-orch"),
    "intake": ("Intake and triage", "--f-intake"),
    "design": ("Designers and critics", "--f-design"),
    "impl": ("Implementers", "--f-impl"),
    "qa": ("Testers", "--f-qa"),
}
INITIALS = {"orch": "OR", "intake": "IN", "design": "DS", "impl": "IM", "qa": "QA"}
SURFACES = {"ui": "UI", "api": "API", "access": "Access", "desktop": "Desktop", "none": "No runtime change"}
SEVERITY_TONES = {"severe": "bad", "major": "warn", "minor": ""}
INBOX_TONES = {"users": "pink", "owner": "violet", "agents": "info", "chat": "amber"}


class KindLook(NamedTuple):
    """How the pages show one kind of file: its name, the name of a group of them, its icon and its tone."""

    name: str
    group: str
    icon: str
    tone: str


KIND_LOOK = {
    "goal": KindLook("Goal", "Goal", "flag", "accent"),
    "task": KindLook("Task", "Tasks", "queue-list", "info"),
    "request": KindLook("Request", "Requests", "chat-bubble-left-right", "amber"),
    "spec": KindLook("Spec", "Specs", "document-text", "violet"),
    "check": KindLook("Check", "Checks", "check-circle", "good"),
    "reflection": KindLook("Reflection", "Reflections", "light-bulb", "pink"),
}
REFLECT_TONES = {
    "miss": "bad",
    "owner-feedback": "accent",
    "friction": "warn",
    "discovery": "good",
    "misroute": "violet",
    "incident": "amber",
}
_MODEL = re.compile(r"^claude-([a-z]+)-(\d+)-(\d+)")


def epoch(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def when(seconds: float, utc: bool = False) -> str:
    """Oct 9, 14:02 (UTC)."""
    moment = datetime.fromtimestamp(seconds, UTC)
    return f"{moment:%b} {moment.day}, {moment:%H:%M}" + (" UTC" if utc else "")


def day_label(day: str) -> str:
    moment = datetime.strptime(day, "%Y-%m-%d")
    return f"{moment:%b} {moment.day}"


def clock(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%H:%M")


def ago(seconds: float, now: float) -> str:
    minutes = max(0, round((now - seconds) / 60))
    if minutes < 60:
        return f"{minutes} min ago"
    hours = round(minutes / 60)
    if hours < 24:
        return f"{hours} h ago"
    days = round(hours / 24)
    return "yesterday" if days == 1 else f"{days} days ago"


def span(seconds: float) -> str:
    """26 min, 3 h 12 min, 3 d 19 h."""
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 24 * 60:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes // 1440} d {minutes % 1440 // 60} h"


def money(usd: float | None) -> str:
    if usd is None:
        return "unknown"
    return "$0" if round(usd, 2) == 0 else f"${usd:,.2f}"


def cost_of(cell: list[float] | None) -> str:
    """A [dollars, unknown calls] total as the owner reads it."""
    if cell is None:
        return "no calls yet"
    usd, unknown = cell
    if unknown and not usd:
        return "unknown"
    # A call of unknown price adds an unknown amount: what is known is the least it cost.
    return ("≥ " if unknown else "") + money(usd)


def model_label(model: str) -> str:
    """claude-opus-5-5 is Opus 5.5."""
    found = _MODEL.match(model)
    return f"{found.group(1).title()} {found.group(2)}.{found.group(3)}" if found else model


def model_word(model: str) -> str:
    """claude-opus-5-5 is Opus."""
    return model_label(model).split(" ")[0]


def brief_url(task_id: str) -> str:
    """The address of the page that shows a task's brief."""
    return f"/api/brief/{task_id}"


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def spelled(n: int) -> str:
    """Two, not 2, in a sentence; a number past nine stays a number."""
    return ("zero one two three four five six seven eight nine".split() + [str(n)])[n if 0 <= n <= 9 else 10]


def ordinal(n: int) -> str:
    suffix = "th" if 10 < n % 100 < 14 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def version(*parts: Any) -> str:
    """What a sheet's action carries, so a stale one is refused: a fingerprint of what the owner saw."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:12]


def page(rows: list[Any], query: Query, size: int, key: str = "page") -> tuple[list[Any], bool]:
    """One page of rows, and whether more follow."""
    raw = query.get(key, "1")
    if not raw.isdigit() or int(raw) < 1:
        raise PartError(f"{key} must be a page number from 1.")
    start = (int(raw) - 1) * size
    return rows[start : start + size], len(rows) > start + size


def broken(file: str, error: str) -> dict[str, Any]:
    return {"broken": True, "id": file, "error": error}


def first_line(text: str, limit: int = 120) -> str:
    line = " ".join(next((x for x in text.splitlines() if x.strip()), "").lstrip("# ").split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def one_of(value: str, allowed: Iterable[str], name: str) -> str:
    if value not in allowed:
        raise PartError(f"{name} must be one of {', '.join(allowed)}.")
    return value
