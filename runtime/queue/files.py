"""The warden's safe reads and writes inside a product's queue/ folder.

The queue sits in a checkout another user owns, so every open walks from the queue's own directory descriptor with
O_NOFOLLOW: a symlink planted on the way is refused, never followed. The queue folder and every folder under it must
be a real directory owned by the warden and writable by no one else, or nothing is read or written. Writes are
atomic: a temporary file beside the target, fsynced, then renamed over it.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import secrets
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path

from runtime.shared.protocol import Refused

QUEUE = "queue"
LOCK = ".lock"
# A queue name never starts with a dot, so a temporary file is never listed or read as an item.
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def _unsafe(reason: str) -> Refused:
    return Refused("queue-unsafe", reason)


def _parts(rel: str) -> list[str]:
    parts = rel.split("/")
    if not parts or not all(NAME.match(p) and p not in (".", "..") for p in parts):
        raise _unsafe(f"{rel!r} is not a queue path")
    return parts


def _check_dir(fd: int, what: str, owner_uid: int) -> None:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise _unsafe(f"{what} is not a directory")
    if info.st_uid != owner_uid:
        raise _unsafe(f"{what} is not owned by the warden: reinstall to fix its owner")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise _unsafe(f"{what} is writable by others: reinstall to fix its mode")


def read_repo_file(repo: Path, name: str, limit: int) -> bytes:
    """A regular file at the top of the checkout, such as GOAL.md, read without following a symlink."""
    if not NAME.match(name):
        raise _unsafe(f"{name!r} is not a file name")
    top = os.open(repo, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        return _read_at(top, name, limit, f"{repo}/{name}")
    finally:
        os.close(top)


def _read_at(dir_fd: int, name: str, limit: int | None, what: str) -> bytes:
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _unsafe(f"{what} is a symlink") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _unsafe(f"{what} is not a regular file")
        chunks, size = [], 0
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
            size += len(chunk)
            if limit is not None and size > limit:
                raise Refused("too-large", f"{what} is larger than {limit // 1024} KiB")
        return b"".join(chunks)
    finally:
        os.close(fd)


class QueueFiles:
    """The queue folder of one checkout, opened only as a warden-owned directory."""

    def __init__(self, repo: Path, owner_uid: int) -> None:
        self.repo = repo
        self.owner_uid = owner_uid

    @contextmanager
    def _root(self) -> Iterator[int]:
        top = os.open(self.repo, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            try:
                fd = os.open(QUEUE, _DIR_FLAGS, dir_fd=top)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise _unsafe(f"{self.repo}/{QUEUE} is not a real directory") from exc
                if exc.errno == errno.ENOENT:
                    raise _unsafe(f"{self.repo}/{QUEUE} is missing: reinstall to create it") from exc
                raise
        finally:
            os.close(top)
        try:
            _check_dir(fd, f"{self.repo}/{QUEUE}", self.owner_uid)
            yield fd
        finally:
            os.close(fd)

    def _walk(self, root: int, parts: list[str], create: bool) -> int:
        """A descriptor for the folder ``parts`` under the queue; the caller closes it."""
        fd = os.dup(root)
        try:
            for i, part in enumerate(parts):
                made = False
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, 0o755, dir_fd=fd)
                        made = True
                try:
                    nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                        raise _unsafe(f"queue/{'/'.join(parts[: i + 1])} is not a real directory") from exc
                    raise
                os.close(fd)
                fd = nxt
                if made:
                    os.fchmod(fd, 0o755)  # the process umask may have left it group-writable
                _check_dir(fd, f"queue/{'/'.join(parts[: i + 1])}", self.owner_uid)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, rel: str) -> bytes:
        """The file's bytes; FileNotFoundError when it is missing."""
        *dirs, name = _parts(rel)
        with self._root() as root:
            fd = self._walk(root, dirs, create=False)
            try:
                return _read_at(fd, name, None, f"queue/{rel}")
            finally:
                os.close(fd)

    def exists(self, rel: str) -> bool:
        try:
            self.read(rel)
        except FileNotFoundError:
            return False
        return True

    def names(self, sub: str) -> list[str]:
        """The item names in a queue folder, sorted; an absent folder has none."""
        with self._root() as root:
            try:
                fd = self._walk(root, _parts(sub), create=False)
            except FileNotFoundError:
                return []
            try:
                return sorted(n for n in os.listdir(fd) if NAME.match(n))
            finally:
                os.close(fd)

    def write(self, rel: str, data: bytes) -> None:
        self.write_many({rel: data})

    def write_many(self, files: Mapping[str, bytes]) -> None:
        """Every file is staged before any is renamed into place, so a refused or failed stage writes nothing."""
        staged: list[tuple[int, str, str]] = []
        dirs: dict[tuple[str, ...], int] = {}
        with self._root() as root:
            try:
                for rel, data in files.items():
                    *parent, name = _parts(rel)
                    key = tuple(parent)
                    if key not in dirs:
                        dirs[key] = self._walk(root, parent, create=True)
                    tmp = f".{name}.{secrets.token_hex(4)}.tmp"
                    fd = os.open(
                        tmp,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o644,
                        dir_fd=dirs[key],
                    )
                    staged.append((dirs[key], tmp, name))
                    try:
                        view = memoryview(data)
                        while view:
                            view = view[os.write(fd, view) :]
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                for dir_fd, tmp, name in staged:
                    os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                staged.clear()
                for dir_fd in dirs.values():
                    os.fsync(dir_fd)
            finally:
                for dir_fd, tmp, _ in staged:
                    with suppress(FileNotFoundError):
                        os.unlink(tmp, dir_fd=dir_fd)
                for dir_fd in dirs.values():
                    os.close(dir_fd)

    def append(self, rel: str, line: bytes) -> None:
        """One line added to the end of a file, created if missing; the caller holds the queue lock."""
        *parent, name = _parts(rel)
        with self._root() as root:
            dir_fd = self._walk(root, parent, create=True)
            try:
                fd = os.open(
                    name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dir_fd
                )
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise _unsafe(f"queue/{rel} is not a regular file")
                    os.write(fd, line if line.endswith(b"\n") else line + b"\n")
                    os.fsync(fd)
                finally:
                    os.close(fd)
            finally:
                os.close(dir_fd)

    @contextmanager
    def lock(self) -> Iterator[None]:
        """The queue's exclusive lock, held across a read and the writes that depend on it."""
        with self._root() as root:
            fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=root)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)
