"""A `slot up` that would change nothing is skipped (slots.same_up_minutes). An up rebuilds every image and recreates
what changed, minutes of CPU on a busy machine, and agents often ask for one again with nothing new. It is skipped
while the checkout, the slot's env file and the up's words are what the slot's last good up had, every container
that up left still runs as it started then, none is unhealthy, and that up is younger than the setting. The checkout
counts as its HEAD's tree, its diff from HEAD and the bytes of each untracked file, so an edit not yet committed counts
too. An up after `factory slot down` builds, since the containers the last one left are gone. When git or docker
cannot say, or the record cannot be read, the up runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from runtime.shared import environment, store

RECORD = "same-up.json"
# Each running container as "<id> <started at> [health]".
_STARTED = "{{.Id}} {{.State.StartedAt}} {{if .State.Health}}{{.State.Health.Status}}{{end}}"
Docker = Callable[[list[str], int], subprocess.CompletedProcess[str]]


class SameUpError(Exception):
    """git, docker or the record could not say what the slot holds."""


def check(state: Path, label: str, docker: Docker, timeout: int, *, checkout: Path, env_file: Path,
          verb: list[str], seconds: int) -> tuple[str, str]:  # fmt: skip
    """This up's stamp ("" when it takes none) and why it would change nothing ("" when it runs)."""
    if seconds <= 0:
        return "", ""
    try:
        stamp = _stamp(checkout, env_file, verb, timeout)
        running = _started(docker, label, timeout)
        last = store.read_json_or(state / RECORD, None)
        kept, at = _record(last) if isinstance(last, dict) and last.get("stamp") == stamp else ([], 0.0)
    # telemetry: surfaced — why the up runs whole is `print`ed to the run's log
    except (SameUpError, store.StoreError) as exc:
        print(f"stack-run: the up runs, as {exc}", flush=True)
        return "", ""
    age = time.time() - at
    now = {" ".join(line.split()[:2]) for line in running}
    sick = any(line.split()[2:] == ["unhealthy"] for line in running)
    if not kept or not set(kept) <= now or sick or age >= seconds:
        return stamp, ""
    same = f"its last good up, {age / 60:.0f} min ago, built this same checkout and env file"
    return stamp, f"{same}, and every container it left still runs; `factory slot down` first to build again"


def record(state: Path, label: str, docker: Docker, timeout: int, stamp: str) -> None:
    """After a good up: its stamp, its time and the containers it left running."""
    try:
        running = _started(docker, label, timeout)
    # telemetry: surfaced — why the next up runs whole is `print`ed to the run's log
    except SameUpError as exc:
        (state / RECORD).unlink(missing_ok=True)
        print(f"stack-run: the next up runs whole, as {exc}", flush=True)
        return
    kept = [" ".join(line.split()[:2]) for line in running]
    store.write_json(state / RECORD, {"stamp": stamp, "at": time.time(), "containers": kept})


def _record(last: dict[str, object]) -> tuple[list[str], float]:
    kept, at = last.get("containers"), last.get("at")
    if not isinstance(kept, list) or not all(isinstance(c, str) for c in kept) or not isinstance(at, int | float):
        raise SameUpError(f"{RECORD} holds no containers and time")
    return kept, float(at)


def _stamp(checkout: Path, env_file: Path, verb: list[str], timeout: int) -> str:
    digest = hashlib.sha256()
    try:
        parts = [json.dumps([str(checkout), verb]).encode(), env_file.read_bytes()]
        for args in (["rev-parse", "HEAD^{tree}"], ["diff", "HEAD", "--binary", "--no-ext-diff", "--no-textconv"]):
            parts.append(_git(checkout, args, timeout))
        untracked = _git(checkout, ["ls-files", "--others", "--exclude-standard", "-z"], timeout)
        for name in filter(None, untracked.split(b"\0")):
            path = checkout / os.fsdecode(name)
            parts += [name, os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()]
    except OSError as exc:
        raise SameUpError(f"the checkout could not be read ({type(exc).__name__})") from exc
    for part in parts:
        digest.update(b"%d:" % len(part) + part)
    return digest.hexdigest()


def _git(checkout: Path, args: list[str], timeout: int) -> bytes:
    # The stack user reads a checkout the agent user owns: git refuses that unless its command line says it is safe.
    argv = ["git", "-c", f"safe.directory={checkout}", "-C", str(checkout), *args]
    env = {**environment.handed_on(), "GIT_OPTIONAL_LOCKS": "0"}
    try:
        done = subprocess.run(argv, env=env, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SameUpError(f"git {args[0]} could not run ({type(exc).__name__})") from exc
    if done.returncode != 0:
        said = done.stderr.decode(errors="replace").strip()[-200:]
        raise SameUpError(f"git {args[0]} exited {done.returncode}: {said}")
    return done.stdout


def _started(docker: Docker, label: str, timeout: int) -> list[str]:
    try:
        found = docker(["container", "ls", "-q", "--filter", label], timeout)
        ids = found.stdout.split()
        told = docker(["container", "inspect", "--format", _STARTED, *ids], timeout) if ids else found
    except subprocess.TimeoutExpired as exc:
        raise SameUpError("docker did not answer in time") from exc
    if found.returncode != 0 or told.returncode != 0:
        raise SameUpError(f"docker could not list the slot's containers: {(found.stderr + told.stderr).strip()[-200:]}")
    return [line for line in told.stdout.splitlines() if line.strip()] if ids else []
