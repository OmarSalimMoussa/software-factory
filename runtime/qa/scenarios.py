"""The standard format of a product's standing QA scenarios, in its qa/scenarios/: the one reader, which the
qa-scenarios check and the replay both use.

A scenario opens with three labels as front matter. ``protects`` names the files or folders whose change could break
it, each of which exists. ``needs`` lists what must be set up first: ``nothing``, ``seed <name>``, ``person <role>``
(someone the tester signs in as) or ``session setup`` (a step the starting session runs). ``runs`` says where:
``in-turn`` (on the default branch, in turn), ``desktop`` (on the slot's remote desktop) or ``waiting`` (never run
until what it needs exists). An ``items`` label may say how many of a tester's items it takes, up to ``qa.max_items``;
without it, one. Then at most ``qa.max_steps`` numbered steps for each item it takes, so its walk fits its items'
time; a waiting scenario may hold more. A step may open with a short bold name (``1. **Search.** …``) and holds what
it expects on a line of its own (``Expect: …`` or ``Expected: …``); the replay shows both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FOLDER = "qa/scenarios"
LABELS = ("protects", "needs", "runs")
ITEMS = "items"
RUNS = ("in-turn", "desktop", "waiting")
NEEDS = re.compile(r"^(nothing|session setup|seed [a-z0-9-]+|person [a-z0-9-]+( [a-z0-9-]+)*)$")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_STEP = re.compile(r"^\d+\. (.*)$")
_NAME = re.compile(r"^\*\*([^*]{1,40}?)[.:]?\*\*\s*")
_EXPECT = re.compile(r"^\s*(?:-\s*)?Expect(?:ed)?:\s*(.+)$")


@dataclass(frozen=True, slots=True)
class Step:
    name: str  # the step's short bold name; "" when it has none
    expected: str  # what it expects; "" when it says nothing it expects


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    protects: tuple[str, ...]
    needs: tuple[str, ...]
    runs: str
    items: int
    steps: tuple[Step, ...]


def steps_of(text: str) -> tuple[Step, ...]:
    """Every numbered step, in order, with its name and the first thing it expects."""
    found: list[Step] = []
    for line in text.splitlines():
        step = _STEP.match(line)
        if step:
            named = _NAME.match(step.group(1))
            found.append(Step(named.group(1).strip() if named else "", ""))
            continue
        said = _EXPECT.match(line)
        if said and found and not found[-1].expected:
            found[-1] = Step(found[-1].name, said.group(1).strip())
    return tuple(found)


def front(text: str) -> dict[str, str] | None:
    """The front matter's labels, or None when the file opens with none."""
    match = _FRONT.match(text)
    if match is None:
        return None
    return {
        key.strip(): value.strip() for key, _, value in (line.partition(":") for line in match.group(1).split("\n"))
    }


def items_of(meta: dict[str, str], max_items: int) -> int | None:
    """How many of a tester's items a scenario takes: one without the label, None when the label is out of bounds."""
    said = meta.get(ITEMS)
    if said is None:
        return 1
    return int(said) if said.isdecimal() and 1 <= int(said) <= max_items else None


def most_steps(taken: int, max_steps: int) -> int:
    return taken * max_steps


def problems(rel: str, text: str, exists: set[str] | None, max_steps: int, max_items: int) -> list[str]:
    """Every way the file breaks the format; ``exists`` answers whether a protected path is there (None: unknown)."""
    meta = front(text)
    if meta is None:
        return [f"{rel}: no front matter (protects, needs, runs) above the title"]
    if not set(LABELS) <= set(meta) <= {*LABELS, ITEMS}:
        return [f"{rel}: front matter has {sorted(meta)}, not protects, needs and runs, with items if it takes more"]
    out = [] if meta["protects"].split() else [f"{rel}: protects names no path"]
    if exists is not None:
        out += [
            f"{rel}: protects {path}, which does not exist"
            for path in meta["protects"].split()
            if path.strip("/") in ("", ".") or path not in exists
        ]
    needs = [need.strip() for need in meta["needs"].split(",")]
    out += [
        f"{rel}: needs {need!r} is not one of: nothing, seed <name>, person <role>, session setup"
        for need in needs
        if not NEEDS.match(need)
    ]
    if "nothing" in needs and len(needs) > 1:
        out.append(f"{rel}: needs lists nothing beside other needs")
    if meta["runs"] not in RUNS:
        out.append(f"{rel}: runs {meta['runs']!r} is not one of {', '.join(RUNS)}")
    return out + _size(rel, text, meta, max_steps, max_items)


def _size(rel: str, text: str, meta: dict[str, str], max_steps: int, max_items: int) -> list[str]:
    taken, count = items_of(meta, max_items), len(steps_of(text))
    out = [] if taken else [f"{rel}: items {meta[ITEMS]!r} is not a whole number from 1 to {max_items}"]
    if not count:
        out.append(f"{rel}: no numbered steps")
    elif taken and count > most_steps(taken, max_steps) and meta["runs"] != "waiting":
        out.append(f"{rel}: {count} steps; {max_steps} at most for each of {taken} item(s): split it, or raise items")
    return out


def parse(name: str, text: str, max_steps: int, max_items: int) -> tuple[Scenario | None, list[str]]:
    """One scenario from its file's text, or None with every problem; whether its protected paths exist is the
    check's to say, from the checkout."""
    found = problems(f"{FOLDER}/{name}.md", text, None, max_steps, max_items)
    meta = front(text)
    if found or meta is None or (taken := items_of(meta, max_items)) is None:
        return None, found
    needs = tuple(need.strip() for need in meta["needs"].split(","))
    return Scenario(name, tuple(meta["protects"].split()), needs, meta["runs"], taken, steps_of(text)), []
