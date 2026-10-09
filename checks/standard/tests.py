"""Tests: the changed code's tests pass, run through the product's own ``scripts/dev test [path...]``.

check-values/test-map.toml says which tests read which paths: each changed path maps, by its longest matching prefix
in ``readers``, to the test paths that can break; a prefix in ``full`` runs every test; a path under no prefix runs
every test and says so (map it). ``always`` joins every selection; a changed file with a ``skip_suffixes`` suffix
selects nothing. --all runs every test. On a shared machine a run starts only when its memory ceiling and the reserve
for every other session are free, and runs in a scope systemd stops at the ceiling (the budget.test_* settings).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from checks.runner.catalog import NOTE
from checks.runner.repo import Repo

ID = "tests"
RULE = "The changed code's tests pass"
VALUES_FILE = "test-map.toml"
VALUES = {"always": [str], "full": [str], "skip_suffixes": [str], "readers": {str: [str]}}

MEMINFO = Path("/proc/meminfo")


def _matches(rel: str, prefix: str) -> bool:
    return rel == prefix or (prefix.endswith("/") and rel.startswith(prefix))


def select(changed: tuple[str, ...], values: dict[str, Any]) -> tuple[list[str] | None, list[str]]:
    """The test paths to run (None for every test) and every changed path the map does not place."""
    picked: set[str] = set(values["always"])
    unplaced: list[str] = []
    everything = False
    for rel in changed:
        if rel.endswith(tuple(values["skip_suffixes"])):
            continue
        if any(_matches(rel, prefix) for prefix in values["full"]):
            everything = True
            continue
        matches = [prefix for prefix in values["readers"] if _matches(rel, prefix)]
        if not matches:
            unplaced.append(rel)
            continue
        picked |= set(values["readers"][max(matches, key=len)])
    return (None if everything or unplaced else sorted(picked)), unplaced


def available_mib() -> int:
    for line in MEMINFO.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise RuntimeError(f"{MEMINFO} has no MemAvailable line")


def _capped(repo: Repo, command: list[str], ceiling: int) -> tuple[int, str]:
    unit = f"factory-test-{os.getpid()}.scope"
    done = subprocess.run(
        [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            f"--unit={unit}",
            f"--property=MemoryMax={ceiling}M",
            "--property=MemorySwapMax=0",
            "--",
            *command,
        ],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (done.stdout + done.stderr).strip()
    if done.returncode == 0:
        return 0, output
    # A scope that ended by itself is gone and reads `success`; one its ceiling stopped stays failed until reset.
    result = subprocess.run(
        ["systemctl", "--user", "show", "--property=Result", "--value", unit],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if result == "oom-kill":
        subprocess.run(["systemctl", "--user", "reset-failed", unit], capture_output=True, check=False)
        return done.returncode, (
            f"{output}\nThe run passed its {ceiling} MiB memory ceiling, so systemd stopped "
            "it: a test now keeps more memory than before; find it before raising the ceiling"
        )
    return done.returncode, output


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    paths: list[str] | None = None
    notes: list[str] = []
    if repo.changed is not None:
        paths, unplaced = select(repo.changed, values)
        notes = [
            f"{NOTE}{rel} matches no prefix in check-values/{VALUES_FILE}, so every test ran: map it"
            for rel in unplaced
        ]
        if paths == []:
            return []
    command = [str(repo.root / "scripts" / "dev"), "test", *(paths or [])]
    if repo.test_memory is None:
        done = subprocess.run(command, cwd=repo.root, capture_output=True, text=True, check=False)
        status, output = done.returncode, (done.stdout + done.stderr).strip()
    else:
        ceiling, reserve = repo.test_memory
        free = available_mib()
        if free < ceiling + reserve:
            return [
                f"{free} MiB of memory is free and a test run needs {ceiling + reserve} MiB (its {ceiling} MiB "
                f"ceiling and {reserve} MiB kept for every other session): free some (stop a slot you do not "
                "use, wait for another check to end), then run again"
            ]
        status, output = _capped(repo, command, ceiling)
    if status != 0:
        return [*notes, f"scripts/dev test {' '.join(paths or [])} failed (exit {status}):\n{output}"]
    return notes
