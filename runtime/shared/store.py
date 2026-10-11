"""Small, safe file primitives: atomic writes, JSON lines, and an exclusive lock.

Every state file is replaced atomically, so a reader never sees half a file. A file that exists but cannot be read
raises StoreError, so the caller decides what to refuse; a missing file is the caller's default, never a guess here.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class StoreError(Exception):
    """A state file exists but cannot be read or holds the wrong shape."""


def write_text(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_inside(root: Path, rel: Path) -> str:
    """The text of ``root``/``rel``, read by root from a folder another user writes: no part of ``rel`` may be a
    link, and the file must be a plain one, so nothing there sends the read elsewhere or blocks it."""
    fds = [os.open(root, os.O_RDONLY | os.O_DIRECTORY)]
    try:
        for part in rel.parts[:-1]:
            fds.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1]))
        fds.append(os.open(rel.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fds[-1]))
        if not stat.S_ISREG(os.fstat(fds[-1]).st_mode):
            raise StoreError(f"{root / rel}: not a plain file")
        with os.fdopen(os.dup(fds[-1]), encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise StoreError(f"{root / rel}: {exc}") from exc
    finally:
        for fd in fds:
            os.close(fd)


def write_json(path: Path, data: Any, mode: int = 0o644) -> None:
    write_text(path, json.dumps(data, indent=1, sort_keys=True) + "\n", mode)


def read_json(path: Path) -> Any:
    """The file's JSON; raises FileNotFoundError when missing and StoreError when unreadable."""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        raise StoreError(f"{path}: {exc}") from exc


def read_json_or(path: Path, missing: Any) -> Any:
    try:
        return read_json(path)
    except FileNotFoundError:
        return missing


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every record; a line that is not a JSON object raises StoreError naming the line."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise StoreError(f"{path}: {exc}") from exc
    out: list[dict[str, Any]] = []
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise StoreError(f"{path}:{n}: {exc}") from exc
        if not isinstance(record, dict):
            raise StoreError(f"{path}:{n}: not an object")
        out.append(record)
    return out


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``path`` (created if missing) for the block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
