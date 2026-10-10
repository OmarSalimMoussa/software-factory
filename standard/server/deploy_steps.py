"""The parts of a deploy that stand alone: syncing the deploy files, reading the health answer, pruning old images."""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import stat
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from standard.config import Config
from standard.services.provision import Run, checked

SHORT = 8
DRAIN_STEP_SECONDS = 10
ANSWER_STEP_SECONDS = 2
HTTP_TIMEOUT_SECONDS = 5
_TAG = re.compile(r"[0-9a-f]{7,40}")
# $$ (a literal $), ${NAME}, ${NAME:-default}, ${NAME-default}, ${NAME:?message}, ${NAME?message}, or $NAME.
_VAR = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-?])([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")

Get = Callable[[str], tuple[int, bytes] | None]
Say = Callable[[str], None]
Now = Callable[[], float]
Sleep = Callable[[float], None]


class DeployError(Exception):
    """The deploy cannot go on; the message says why and what to do."""


def http_get(url: str) -> tuple[int, bytes] | None:
    """The status and body of an answer, or None when nothing answers."""
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return int(response.status), response.read(65536)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536)
    except (OSError, http.client.HTTPException):
        return None


def sync(source: Path, target: Path, keep: set[str]) -> set[str]:
    """Make ``target`` hold exactly ``source``'s files, in place; return the relative paths that changed.

    Files are overwritten, never swapped: a container's bind mount holds a file or folder by inode, and a swapped-in
    copy would leave it reading the old one for good. ``keep`` names top-level entries that stay though source lacks
    them.
    """
    wanted: set[Path] = set()
    changed: set[str] = set()
    target.mkdir(parents=True, exist_ok=True)
    for here, folders, files in os.walk(source):
        rel = Path(here).relative_to(source)
        for name in folders + files:
            src, dst = Path(here) / name, target / rel / name
            if src.is_symlink() or not (src.is_dir() or src.is_file()):
                raise DeployError(f"the deploy image holds {rel / name}, which is not a plain file or folder")
            wanted.add(rel / name)
            if dst.is_symlink() or (dst.exists() and dst.is_dir() != src.is_dir()):
                _remove(dst)
            if src.is_dir():
                dst.mkdir(exist_ok=True)
            elif not dst.exists() or dst.read_bytes() != src.read_bytes():
                with src.open("rb") as reader, dst.open("wb") as writer:
                    shutil.copyfileobj(reader, writer)
                changed.add((rel / name).as_posix())
            os.chmod(dst, stat.S_IMODE(src.stat().st_mode))
    for here, folders, files in os.walk(target, topdown=False):
        rel = Path(here).relative_to(target)
        for name in folders + files:
            if rel / name not in wanted and not (rel == Path(".") and name in keep):
                _remove(target / rel / name)
                changed.add((rel / name).as_posix())
    return changed


def touched(binds: tuple[str, ...], changed: set[str]) -> bool:
    """Whether a service mounting ``binds`` reads one of the ``changed`` paths (a bind may name a folder)."""
    return any(path == bind or path.startswith(bind.rstrip("/") + "/") for bind in binds for path in changed)


def resolve(text: str, values: dict[str, str]) -> str:
    """``text`` with compose's variable references filled from ``values``; a missing required one is an error."""

    def one(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        name, op, default = match.group(1) or match.group(4), match.group(2), match.group(3)
        value = values.get(name)
        if (op in (":-", ":?") and not value) or (op in ("-", "?") and value is None):
            if op.endswith("-"):
                return default or ""
            raise DeployError(f"compose names a network with ${{{name}}}, which the env file does not set")
        if value is None:
            raise DeployError(f"compose names a network with ${name}, which the env file does not set")
        return value

    return _VAR.sub(one, text)


def drain(url: str, minutes: int, get: Get, now: Now, sleep: Sleep, say: Say) -> None:
    """Wait, bounded, while the running stack reports busy work; nothing answering means nothing runs."""
    deadline = now() + minutes * 60
    while True:
        answer = get(url)
        if answer is None:
            say(f"drain: nothing answers at {url}, so nothing is running")
            return
        status, body = answer
        busy = field(body, "busy")
        if isinstance(busy, bool) or not isinstance(busy, int) or busy < 0:
            if 200 <= status < 300:
                raise DeployError(
                    f"{url} answered without a whole-number busy count: {body[:200]!r}; "
                    "the product's health answer must say how much work is running"
                )
            say(f"drain: {url} answers {status}, so nothing healthy is running")
            return
        if busy == 0:
            say("drain: nothing is busy")
            return
        if now() >= deadline:
            say(f"warning: {busy} jobs are still busy after {minutes} min; going on")
            return
        say(f"drain: waiting for {busy} busy jobs to end")
        sleep(DRAIN_STEP_SECONDS)


def answer(url: str, want: str, seconds: int, get: Get, now: Now, sleep: Sleep, say: Say) -> None:
    """Wait until the health answer names ``want`` (``<workspace> at <short commit>``), or fail naming what did."""
    deadline = now() + seconds
    while True:
        found = get(url)
        got = "nothing" if found is None else f"{field(found[1], 'workspace')} at {field(found[1], 'build_commit')}"
        if got == want:
            say(f"answer: {url} answers as {want}")
            return
        if now() >= deadline:
            raise DeployError(f"{url} answers {got}, not {want}, after {seconds} s")
        sleep(ANSWER_STEP_SECONDS)


def prune(run: Run, config: Config, product_dir: Path, say: Say) -> None:
    """Remove product images no workspace needs: each keeps its last ``keep_images`` commits; in-use ones stay."""
    count = config.deploy.keep_images
    keep: set[str] = set()
    for state in sorted(product_dir.glob("*/state")):
        keep.update(_recent(state / "history.jsonl", count))
        if (state / "deployed").exists():
            keep.add((state / "deployed").read_text(encoding="utf-8").strip()[:SHORT])
    used = set(checked(run, ["docker", "ps", "-a", "--format", "{{.Image}}"]).split())
    prefix = f"{config.product.registry}/{config.product.name}-"
    removed = 0
    for line in checked(run, ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}\t{{.ID}}"]).splitlines():
        ref, _, image_id = line.partition("\t")
        tag = ref.rsplit(":", 1)[-1]
        if not ref.startswith(prefix) or not _TAG.fullmatch(tag) or tag[:SHORT] in keep or {ref, image_id} & used:
            continue
        checked(run, ["docker", "image", "rm", ref])
        removed += 1
    checked(run, ["docker", "image", "prune", "--force"])
    say(f"images: removed {removed} old tags; each workspace keeps its last {count} commits")


def field(body: bytes, key: str) -> Any:
    """One key of a JSON object body, or None when the body is not one or lacks it."""
    try:
        found = json.loads(body)
    except ValueError:
        return None
    return found.get(key) if isinstance(found, dict) else None


def _recent(history: Path, count: int) -> list[str]:
    if not history.exists():
        return []
    commits: list[str] = []
    for number, line in reversed(list(enumerate(history.read_text(encoding="utf-8").splitlines(), start=1))):
        try:
            commit = str(json.loads(line)["commit"])[:SHORT]
        except (ValueError, KeyError, TypeError) as exc:
            raise DeployError(f"{history} line {number} is not a deploy record ({type(exc).__name__})") from exc
        if commit not in commits:
            commits.append(commit)
        if len(commits) == count:
            break
    return commits


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
