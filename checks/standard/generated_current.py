"""Generated current: generated files match what their generator writes.

A generated file edited by hand, or left behind when its source changed, drifts from the generator nothing reruns.
Two kinds of generator are listed. A check command runs in the repo root and exits non-zero when a file it writes is
stale (``generate --check``). A print command writes the file's content to its standard output, and the committed
file must equal it byte for byte. A command that cannot start fails as loudly as a stale file.
"""

from __future__ import annotations

import subprocess
from typing import Any

from checks.runner.repo import Repo

ID = "generated-current"
RULE = "Generated files match what their generator writes"
VALUES_FILE = "generated-current.toml"
VALUES = {
    "check_commands": [{"name": str, "command": [str]}],
    "print_commands": [{"name": str, "command": [str], "file": str}],
}


def _run(repo: Repo, name: str, command: list[str]) -> tuple[subprocess.CompletedProcess[str] | None, str]:
    if not command:
        return None, f"{ID}: {name}: its command is empty"
    try:
        done = subprocess.run(command, cwd=repo.root, capture_output=True, text=True, check=False)
    # telemetry: surfaced — a command that cannot start comes back from `_run` as the check's problem
    except OSError as exc:
        return None, f"{ID}: {name}: `{' '.join(command)}` cannot start: {exc.strerror or exc}"
    return done, ""


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for row in values["check_commands"]:
        done, failure = _run(repo, row["name"], row["command"])
        if done is None:
            problems.append(failure)
        elif done.returncode != 0:
            said = (done.stdout + done.stderr).strip()
            problems.append(f"{ID}: {row['name']}: `{' '.join(row['command'])}` says a generated file is stale "
                            f"(exit {done.returncode}); run the generator and commit what it writes"
                            + (f"\n{said}" if said else ""))  # fmt: skip
    for row in values["print_commands"]:
        target = repo.root / row["file"]
        if not target.is_file():
            problems.append(f"{ID}: {row['name']}: {row['file']} is missing")
            continue
        done, failure = _run(repo, row["name"], row["command"])
        if done is None:
            problems.append(failure)
        elif done.returncode != 0:
            problems.append(f"{ID}: {row['name']}: `{' '.join(row['command'])}` failed (exit {done.returncode}): "
                            f"{done.stderr.strip()}")  # fmt: skip
        elif target.read_text(encoding="utf-8") != done.stdout:
            problems.append(f"{ID}: {row['name']}: {row['file']} differs from what `{' '.join(row['command'])}` "
                            f"writes; write its output to {row['file']} and commit it")  # fmt: skip
    return problems
