"""The standard format of a product's standing QA scenarios, in its qa/scenarios/: the one reader, which the
qa-scenarios check and the replay both use.

A scenario opens with three labels as front matter. ``protects`` names the files or folders whose change could break
it, each of which exists. ``needs`` lists what must be set up first: ``nothing``, ``seed <name>``, ``person <role>``
(someone the tester signs in as) or ``session setup`` (a step the starting session runs). ``runs`` says where:
``in-turn`` (on the default branch, in turn), ``desktop`` (on the slot's remote desktop) or ``waiting`` (never run
until what it needs exists). Then at most ``qa.max_steps`` numbered steps, so one scenario fits one tester's time
for an item; a waiting scenario may hold more. A step may open with a short bold name (``1. **Search.** …``) and
holds what it expects on a line of its own (``Expect: …`` or ``Expected: …``); the replay shows both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FOLDER = "qa/scenarios"
LABELS = ("protects", "needs", "runs")
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


def problems(rel: str, text: str, exists: set[str] | None, max_steps: int) -> list[str]:
    """Every way the file breaks the format; ``exists`` answers whether a protected path is there (None: unknown)."""
    meta = front(text)
    if meta is None:
        return [f"{rel}: no front matter (protects, needs, runs) above the title"]
    if set(meta) != set(LABELS):
        return [f"{rel}: front matter has {sorted(meta)}, not protects, needs and runs"]
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
    count = len(steps_of(text))
    if not count:
        out.append(f"{rel}: no numbered steps")
    elif count > max_steps and meta["runs"] != "waiting":
        out.append(f"{rel}: {count} steps; split it, {max_steps} at most")
    return out


def parse(name: str, text: str, max_steps: int) -> tuple[Scenario | None, list[str]]:
    """One scenario from its file's text, or None with every problem; whether its protected paths exist is the
    check's to say, from the checkout."""
    found = problems(f"{FOLDER}/{name}.md", text, None, max_steps)
    meta = front(text)
    if found or meta is None:
        return None, found
    needs = tuple(need.strip() for need in meta["needs"].split(","))
    return Scenario(name, tuple(meta["protects"].split()), needs, meta["runs"], steps_of(text)), []
