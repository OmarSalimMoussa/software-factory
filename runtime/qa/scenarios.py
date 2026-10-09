"""The standard format of a product's standing QA scenarios, in its qa/scenarios/ (factory spec, "Briefs").

Each scenario opens with three labels: ``protects`` (the paths whose change it guards), ``needs`` (what a tester
sets up first) and ``runs`` (``web``, or ``desktop`` for steps outside the web UI), then at most ``qa.max_steps``
numbered steps, so one scenario fits one tester's time for an item.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

FOLDER = "qa/scenarios"
LABELS = ("protects", "needs", "runs")
RUNS = ("web", "desktop")
NEED = re.compile(r"^(nothing|sign-in|seed [a-z0-9-]+|[a-z][a-z0-9 -]{0,40})$")
STEP = re.compile(r"^\d+\. ", re.M)
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    protects: tuple[str, ...]
    needs: tuple[str, ...]
    runs: str
    steps: int

    def touches(self, paths: Iterable[str]) -> bool:
        return any(path == p or path.startswith(p.rstrip("/") + "/") for path in paths for p in self.protects)


def parse(name: str, text: str, max_steps: int) -> tuple[Scenario | None, list[str]]:
    """One scenario from its file's text, or None with every problem."""
    rel = f"{FOLDER}/{name}.md"
    match = _FRONT.match(text)
    if not match:
        return None, [f"{rel}: no front matter (protects, needs, runs) above the title"]
    pairs = [line.partition(":") for line in match.group(1).split("\n")]
    meta = {key.strip(): value.strip() for key, sep, value in pairs if sep}
    problems: list[str] = []
    if sorted(meta) != sorted(LABELS) or len(pairs) != len(LABELS):
        return None, [f"{rel}: front matter is exactly protects, needs and runs"]
    protects = tuple(meta["protects"].split())
    needs = tuple(n.strip() for n in meta["needs"].split(","))
    if not protects:
        problems.append(f"{rel}: protects names no path")
    problems += [
        f"{rel}: needs '{n}' is not nothing, sign-in, seed <name> or a short phrase" for n in needs if not NEED.match(n)
    ]
    if "nothing" in needs and len(needs) > 1:
        problems.append(f"{rel}: needs lists nothing beside other needs")
    if meta["runs"] not in RUNS:
        problems.append(f"{rel}: runs '{meta['runs']}' is not one of {', '.join(RUNS)}")
    steps = len(STEP.findall(text))
    if not steps:
        problems.append(f"{rel}: no numbered steps")
    elif steps > max_steps:
        problems.append(f"{rel}: {steps} steps; split it, {max_steps} at most")
    if problems:
        return None, problems
    return Scenario(name, protects, needs, meta["runs"], steps), []


def load(root: Path, max_steps: int) -> tuple[list[Scenario], list[str]]:
    """Every scenario under the checkout's qa/scenarios/, and every problem, including a protected path that is
    not there."""
    scenarios: list[Scenario] = []
    problems: list[str] = []
    for path in sorted((root / FOLDER).glob("*.md")):
        if path.name == "README.md":
            continue
        found, wrong = parse(path.stem, path.read_text(encoding="utf-8"), max_steps)
        problems += wrong
        if found is not None:
            missing = [p for p in found.protects if not (root / p).exists()]
            problems += [f"{FOLDER}/{path.name}: protects {p}, which does not exist" for p in missing]
            if not missing:
                scenarios.append(found)
    return scenarios, problems
