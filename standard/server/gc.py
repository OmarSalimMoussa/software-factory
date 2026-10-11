"""The weekly clean-up of a server's Docker build cache and dangling images; cron runs it as the admin user.

It sweeps by age, because nothing on a server honours a size ceiling: `docker builder prune --max-used-space` is
accepted and frees nothing, and `builder.gc` in daemon.json validates and caps nothing. `--all` is load-bearing with
the `until` filter: without it the sweep reaches only cache no image shares, a small slice of the total.

Never add `docker volume prune`: compose leaves a volume unreferenced while a deploy replaces its container, and the
volumes are the products' databases, the shared services' data and the edge's certificates. Never add
`docker image prune -a`: it removes every image no running container uses, so the next deploy or rollback re-pulls
everything. `docker system prune` reaches both. Old product images go at each deploy, which keeps the last few.
"""

from __future__ import annotations

import re
import shutil
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from standard.services.provision import Run, real_run

CACHE_MAX_AGE = "168h"
LOG = Path("/srv/factory/state/gc.log")


class _Failed(Exception):
    pass


def last_failure(log: Path) -> str | None:
    """What the last sweep said when it failed; None when it passed or never ran. Every deploy repeats it."""
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    # telemetry: expected — no log yet: the sweep never ran, so no failure is there to repeat
    except FileNotFoundError:
        return None
    last = lines[-1] if lines else ""
    return last if " FAILED at " in last else None


def _ok(run: Run, argv: list[str], step: str) -> str:
    done = run(argv, None)
    if done.returncode != 0:
        said = (done.stderr.strip() or done.stdout.strip() or f"exit code {done.returncode}").splitlines()[-1]
        raise _Failed(f"{step}: {said}")
    return done.stdout


def _images(run: Run) -> int:
    return len(_ok(run, ["docker", "images", "--quiet"], "counting images").split())


def _cache(run: Run) -> str:
    # `docker system df` counts only the unshared part of the cache and understates it many times over.
    found = re.search(r"^Total:\s+(\S+)", _ok(run, ["docker", "builder", "du"], "reading the build cache"), re.M)
    if found is None:
        raise _Failed("reading the build cache: `docker builder du` printed no Total line")
    return found.group(1)


def sweep(run: Run, *, log: Path, root: Path, now: Callable[[], datetime]) -> int:
    def write(line: str) -> None:
        stamped = f"{now().strftime('%Y-%m-%dT%H:%M:%SZ')} {line}"
        print(stamped)
        with log.open("a") as out:
            out.write(stamped + "\n")

    try:
        images_before, cache_before = _images(run), _cache(run)
        prune = ["docker", "builder", "prune", "--all", "--force", "--filter", f"until={CACHE_MAX_AGE}"]
        _ok(run, prune, "pruning the build cache")
        _ok(run, ["docker", "image", "prune", "--force"], "pruning dangling images")
        images_after, cache_after = _images(run), _cache(run)
    except _Failed as exc:
        # telemetry: surfaced — the server has no mail: a failed sweep goes to the log through `write`
        write(f"FAILED at {exc}")
        return 1
    free = shutil.disk_usage(root).free / 1e9
    write(
        f"images {images_before}->{images_after} | cache {cache_before}->{cache_after} (dropped >{CACHE_MAX_AGE})"
        f" | root free {free:.1f}GB"
    )
    return 0


def main() -> int:
    return sweep(real_run, log=LOG, root=Path("/"), now=lambda: datetime.now(UTC))


if __name__ == "__main__":
    sys.exit(main())
