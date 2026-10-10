from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from standard.server import gc
from standard.services.provision import Run

NOW = datetime(2026, 10, 11, 4, 17, tzinfo=UTC)


def _docker(failing: str = "") -> tuple[list[list[str]], Run]:
    calls: list[list[str]] = []
    images = iter(["a\nb\nc\n", "a\n"])
    cache = iter(["ID  RECLAIMABLE\nTotal:\t\t7.4GB\n", "Total:\t\t0B\n"])

    def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if failing and failing in argv:
            return subprocess.CompletedProcess(argv, 1, "", "Error response from daemon: busy")
        out = next(images) if argv[1] == "images" else next(cache) if argv[1:] == ["builder", "du"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    return calls, run


def test_the_sweep_drops_old_cache_and_dangling_images_only_and_logs_a_line(tmp_path: Path) -> None:
    calls, run = _docker()
    log = tmp_path / "gc.log"

    assert gc.sweep(run, log=log, root=tmp_path, now=lambda: NOW) == 0

    assert ["docker", "builder", "prune", "--all", "--force", "--filter", "until=168h"] in calls
    assert ["docker", "image", "prune", "--force"] in calls
    assert not [argv for argv in calls if "volume" in argv or "system" in argv or "-a" in argv]
    assert log.read_text().startswith("2026-10-11T04:17:00Z images 3->1 | cache 7.4GB->0B (dropped >168h) | root free ")


def test_a_failed_step_is_logged_as_failed(tmp_path: Path) -> None:
    calls, run = _docker(failing="prune")
    log = tmp_path / "gc.log"

    assert gc.sweep(run, log=log, root=tmp_path, now=lambda: NOW) == 1

    assert (
        log.read_text() == "2026-10-11T04:17:00Z FAILED at pruning the build cache: Error response from daemon: busy\n"
    )
    assert calls[-1][:3] == ["docker", "builder", "prune"]
