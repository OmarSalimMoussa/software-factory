"""Seeds current: ``scripts/dev seed check`` passes.

The saved inputs testers start from must still load in today's format, so a format change cannot merge while a seed
fails. The product's own script decides what checking a seed means; this check runs it in the repo root and fails
with what it printed when it exits non-zero.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from checks.runner.repo import Repo

ID = "seeds-current"
RULE = "scripts/dev seed check passes"
VALUES_FILE = None
VALUES = None

SCRIPT = "scripts/dev"


def run(repo: Repo, values: dict[str, Any]) -> list[str]:
    script = repo.root / SCRIPT
    if not script.is_file() or not os.access(script, os.X_OK):
        return [f"{ID}: {SCRIPT} is missing or not executable"]
    try:
        done = subprocess.run([str(script), "seed", "check"], cwd=repo.root, capture_output=True, text=True,
                              check=False)  # fmt: skip
    except OSError as exc:
        return [f"{ID}: {SCRIPT} seed check cannot start: {exc.strerror or exc}"]
    if done.returncode == 0:
        return []
    said = (done.stdout + done.stderr).strip()
    return [f"{ID}: {SCRIPT} seed check failed (exit {done.returncode}): convert the seed in this branch"
            + (f"\n{said}" if said else "")]  # fmt: skip
