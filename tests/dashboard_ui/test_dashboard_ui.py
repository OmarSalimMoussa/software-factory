"""Runs the headless dashboard drives against the fixture server; skipped where Chrome or Playwright core is missing."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CHROME = os.environ.get("CHROME", "/usr/bin/google-chrome")


def playwright_found() -> bool:
    if os.environ.get("PLAYWRIGHT_CORE"):
        return True
    base = Path.home() / ".npm/_npx"
    return base.is_dir() and any((d / "node_modules/playwright-core").is_dir() for d in base.iterdir())


@pytest.mark.skipif(
    not (shutil.which("node") and Path(CHROME).is_file() and playwright_found()),
    reason="needs node, Chrome and Playwright core",
)
def test_every_drive_passes() -> None:
    done = subprocess.run(["node", str(HERE / "run.cjs")], capture_output=True, text=True, timeout=900, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
