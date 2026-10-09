"""QA scenarios: every file in qa/scenarios/ has its three labels and at most six steps.

The labels open the file as front matter. ``protects`` names the files or folders whose change could break the
scenario, each of which exists. ``needs`` lists what must be set up first: ``nothing``, ``seed <name>``,
``person <role>`` (someone the tester signs in as) or ``session setup`` (a step the starting session runs). ``runs``
says where: ``in-turn`` (on the default branch, in turn), ``desktop`` (on the slot's remote desktop) or ``waiting``
(never run until what it needs exists). A scenario is one item of a tester's brief, so six numbered steps at most.
"""

from __future__ import annotations

import re
from typing import Any

from checks.runner.repo import Repo

ID = "qa-scenarios"
RULE = "Scenarios have their three labels and at most six steps"
VALUES_FILE = None
VALUES = None

FOLDER = "qa/scenarios"
MAX_STEPS = 6
LABELS = ("protects", "needs", "runs")
RUNS = ("in-turn", "desktop", "waiting")
NEEDS_RE = re.compile(r"^(nothing|session setup|seed [a-z0-9-]+|person [a-z0-9-]+( [a-z0-9-]+)*)$")
_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)
_STEP_RE = re.compile(r"^\d+\. ", re.M)


def _label_problems(repo: Repo, rel: str, meta: dict[str, str]) -> list[str]:
    if set(meta) != set(LABELS):
        return [f"{rel}: front matter has {sorted(meta)}, not protects, needs and runs"]
    out = [] if meta["protects"].split() else [f"{rel}: protects names no path"]
    out += [
        f"{rel}: protects {path}, which does not exist"
        for path in meta["protects"].split()
        if path.strip("/") in ("", ".") or not (repo.root / path).exists()
    ]
    needs = [need.strip() for need in meta["needs"].split(",")]
    out += [
        f"{rel}: needs {need!r} is not one of: nothing, seed <name>, person <role>, session setup"
        for need in needs
        if not NEEDS_RE.match(need)
    ]
    if "nothing" in needs and len(needs) > 1:
        out.append(f"{rel}: needs lists nothing beside other needs")
    if meta["runs"] not in RUNS:
        out.append(f"{rel}: runs {meta['runs']!r} is not one of {', '.join(RUNS)}")
    return out


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for rel in sorted(r for r in repo.tracked if r.startswith(f"{FOLDER}/") and r.endswith(".md")):
        if rel.count("/") != FOLDER.count("/") + 1 or rel.endswith("/README.md"):
            continue
        text = repo.read(rel)
        match = _FRONT_RE.match(text)
        if match is None:
            problems.append(f"{rel}: no front matter (protects, needs, runs) above the title")
            continue
        meta = {
            key.strip(): value.strip()
            for key, _, value in (line.partition(":") for line in match.group(1).splitlines())
        }
        found = _label_problems(repo, rel, meta)
        steps = len(_STEP_RE.findall(text))
        if not steps:
            found.append(f"{rel}: no numbered steps")
        elif steps > MAX_STEPS and meta.get("runs") != "waiting":
            found.append(f"{rel}: {steps} steps; split it, {MAX_STEPS} at most")
        problems += found
    return problems
