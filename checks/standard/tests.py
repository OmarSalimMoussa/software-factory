"""Tests: the changed code's tests pass, run through the product's own ``scripts/dev test [path...]``.

check-values/test-map.toml says which tests read which paths: each changed path maps, by its longest matching prefix
in ``readers``, to the test paths that can break; a prefix in ``full`` runs every test; a path under no prefix runs
every test and says so (map it). ``always`` joins every selection; a changed file with a ``skip_suffixes`` suffix
selects nothing. --all runs every test. On a shared machine a run starts only when its memory ceiling and the reserve
for every other session are free (the budget.test_* settings); the check's own unit holds that ceiling, and a test
that passes it is killed. There the run also stops after checks.run_minutes, and scripts/dev test gets
checks.test_seconds as FACTORY_TEST_SECONDS, the time one test may take, for the product's test runner to enforce.
Its output and errors come back as one stream, in the order printed: an agent sees the report's end, where the test
runner's summary is.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from checks.runner.catalog import NOTE
from checks.runner.repo import Repo
from runtime.shared import environment

ID = "tests"
RULE = "The changed code's tests pass"
VALUES_FILE = "test-map.toml"
VALUES = {"always": [str], "full": [str], "skip_suffixes": [str], "readers": {str: [str]}}

MEMINFO = Path("/proc/meminfo")
CGROUPS = Path("/sys/fs/cgroup")


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


def _oom_kills() -> int:
    """How many processes the kernel killed in this run's own cgroup for passing its memory ceiling; 0 off systemd."""
    try:
        own = Path("/proc/self/cgroup").read_text().strip().splitlines()[-1].split("::", 1)[1]
        events = (CGROUPS / own.lstrip("/") / "memory.events").read_text()
    # telemetry: expected — a run outside a cgroup v2 unit (a developer's own machine) has no ceiling to pass
    except (OSError, IndexError):
        return 0
    return next((int(line.split()[1]) for line in events.splitlines() if line.startswith("oom_kill ")), 0)


def _run(repo: Repo, command: list[str]) -> tuple[int, str]:
    if repo.test_seconds is None:
        done = subprocess.run(command, cwd=repo.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              check=False)  # fmt: skip
        return done.returncode, done.stdout.strip()
    per_test, whole = repo.test_seconds
    env = {**environment.handed_on(), "FACTORY_TEST_SECONDS": str(per_test)}
    killed = _oom_kills()
    try:
        done = subprocess.run(command, cwd=repo.root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=whole, check=False)  # fmt: skip
    # telemetry: surfaced — the stopped run comes back from `_run` as a failure with what it printed
    except subprocess.TimeoutExpired as exc:
        said = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace")
        return 1, f"{said.strip()}\nThe test run passed {whole} s, so it was stopped: find the test that hangs"
    output = done.stdout.strip()
    if done.returncode != 0 and _oom_kills() > killed and repo.test_memory is not None:
        output += (f"\nThe run passed its {repo.test_memory[0]} MiB memory ceiling, so the kernel stopped a test: "
                   "a test now keeps more memory than before; find it before raising the ceiling")  # fmt: skip
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
    if repo.test_memory is not None:
        ceiling, reserve = repo.test_memory
        free = available_mib()
        if free < ceiling + reserve:
            return [
                f"{free} MiB of memory is free and a test run needs {ceiling + reserve} MiB (its {ceiling} MiB "
                f"ceiling and {reserve} MiB kept for every other session): free some (stop a slot you do not "
                "use, wait for another check to end), then run again"
            ]
    status, output = _run(repo, command)
    if status != 0:
        return [*notes, f"scripts/dev test {' '.join(paths or [])} failed (exit {status}):\n{output}"]
    return notes
