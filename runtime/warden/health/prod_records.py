"""`factory records`: one file, or one folder level, of prod's records, read by the warden for an implementer or a
designer, and logged in its task.

The product names the folders agents may read in its factory.toml's [records]: each starts with one of prod's
volumes, and * stands for any one folder. The warden reads the volume's files on this machine itself, with the
read-only capability its unit holds, so no agent reaches prod. Compose names a volume <project>_<volume>, and prod's
project is <product>-prod. A path names folders joined by /, never .., nor a leading /, and no link is followed at
any step: prod's code writes these folders, and a link could point anywhere on the machine.

A file is read whole when it is text of at most telemetry.max_kib. A folder lists at most telemetry.max_items
entries, newest first; above a declared folder, it lists only the folders that lead into one. Before it reads, the
warden adds a line to the reader's task log naming the session and the path, never what the file holds.
"""

from __future__ import annotations

import errno
import os
import stat
from datetime import UTC, datetime
from typing import Any

from runtime.queue import files
from runtime.queue.book import Queue
from runtime.shared.models import Session
from runtime.shared.protocol import Refused, ok
from runtime.warden.context import Context
from runtime.warden.health import probes
from runtime.warden.sessions import registry
from standard.config_records import Config

EVENT = "prod-record"
ANY = "*"
DATA = "_data"
FOLDER, FILE, LINK = "folder", "file", "link"
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_KINDS = ((stat.S_ISDIR, FOLDER), (stat.S_ISREG, FILE), (stat.S_ISLNK, LINK))
_SAID = {errno.ENOENT: "is not there", errno.ELOOP: "is a link, which is never followed",
         errno.ENOTDIR: "is not a folder"}  # fmt: skip


def _refused(reason: str) -> Refused:
    return Refused("records", reason)


def records(ctx: Context, args: dict[str, Any], data: bytes | None) -> dict[str, Any]:
    session, product = _declared(ctx)
    path = args.get("path", "")
    if not path:
        return ok(folders=list(product.records))
    names = _names(path)
    inside, leads = _place(names, [folder.split("/") for folder in product.records])
    outside = f"{path} is outside the folders this product lets agents read: {', '.join(product.records)}"
    if not inside and not leads:
        raise _refused(outside)
    disk = [f"{product.project('prod')}_{names[0]}", DATA, *names[1:]]
    parent = _walk(ctx, disk[:-1], names)
    try:
        kind = _kind(_mode(parent, disk, names))
        settings = ctx.settings()
        if kind == FOLDER:
            _log(ctx, session, f"listed prod's records in {path}")
            return _listing(parent, disk[-1], None if inside else leads, settings.get_int("telemetry.max_items"))
        if kind != FILE:
            raise _refused(f"{path} {_SAID[errno.ELOOP] if kind == LINK else 'is not a file or a folder'}")
        if not inside:
            raise _refused(outside)
        _log(ctx, session, f"read prod's record {path}")
        return _text(parent, disk[-1], path, settings.get_int("telemetry.max_kib") * 1024)
    finally:
        os.close(parent)


def _declared(ctx: Context) -> tuple[Session, Config]:
    session = registry.read(ctx.places).get(ctx.caller.session)
    if session is None or not session.task:
        raise _refused("prod's records are read for a task: ask from the session that works on it")
    try:
        product = probes.product(ctx)
    except probes.NoAddress as exc:
        raise _refused(str(exc)) from exc
    if not product.records:
        raise _refused("this product names no folder of prod's records that agents may read: its owner names them "
                       "in factory.toml's [records]")  # fmt: skip
    return session, product


def _names(path: Any) -> list[str]:
    names = path.removesuffix("/").split("/") if isinstance(path, str) else []
    if not names or not all(files.NAME.match(name) for name in names):
        raise _refused(f"{path!r} is not a path inside prod's records: name folders joined by /, as `factory "
                       "records` lists them, with no .. and no leading /")  # fmt: skip
    return names


def _place(names: list[str], folders: list[list[str]]) -> tuple[bool, set[str]]:
    """Whether ``names`` is inside a declared folder, and else the names that lead from it into one."""
    inside, leads = False, set()
    for folder in folders:
        depth = min(len(folder), len(names))
        if not all(want in (ANY, name) for want, name in zip(folder[:depth], names[:depth], strict=True)):
            continue
        if len(names) >= len(folder):
            inside = True
        else:
            leads.add(folder[depth])
    return inside, leads


def _refuse_known(exc: OSError, code: int, names: list[str], step: int) -> None:
    if step <= 1 and code == errno.ENOENT:
        raise _refused(f"prod has no volume {names[0]}") from exc
    if code in _SAID:
        raise _refused(f"{'/'.join(names[: max(step, 1)])} {_SAID[code]}") from exc


def _walk(ctx: Context, disk: list[str], names: list[str]) -> int:
    fd = os.open(ctx.places.docker_volumes, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    for step, name in enumerate(disk):
        try:
            nxt = os.open(name, _DIR, dir_fd=fd)
        except OSError as exc:
            # Linux answers ENOTDIR, not ELOOP, for a link opened as a folder without following it.
            link = exc.errno == errno.ENOTDIR and _kind(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode) == LINK
            os.close(fd)
            _refuse_known(exc, errno.ELOOP if link else exc.errno or 0, names, step)
            raise
        os.close(fd)
        fd = nxt
    return fd


def _mode(parent: int, disk: list[str], names: list[str]) -> int:
    try:
        return os.stat(disk[-1], dir_fd=parent, follow_symlinks=False).st_mode
    except OSError as exc:
        _refuse_known(exc, exc.errno or 0, names, len(disk) - 1)
        raise


def _kind(mode: int) -> str:
    return next((kind for test, kind in _KINDS if test(mode)), "other")


def _log(ctx: Context, session: Session, text: str) -> None:
    queue = Queue.of(ctx)
    with queue.lock():
        queue.log(session.task, ctx.clock.iso(), session.name, EVENT, f"{session.name} {text}")


def _listing(parent: int, name: str, only: set[str] | None, limit: int) -> dict[str, Any]:
    fd = os.open(name, _DIR, dir_fd=parent)
    try:
        with os.scandir(fd) as found:
            entries = [(entry.name, entry.stat(follow_symlinks=False)) for entry in found]
    finally:
        os.close(fd)
    if only is not None:
        entries = [(n, s) for n, s in entries if stat.S_ISDIR(s.st_mode) and (ANY in only or n in only)]
    entries.sort(key=lambda entry: (-entry[1].st_mtime, entry[0]))
    rows = [{"name": n, "kind": _kind(s.st_mode),
             "changed": datetime.fromtimestamp(s.st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
             **({"bytes": s.st_size} if stat.S_ISREG(s.st_mode) else {})} for n, s in entries[:limit]]  # fmt: skip
    return ok(entries=rows, more=len(entries) > limit)


def _text(parent: int, name: str, path: str, limit: int) -> dict[str, Any]:
    try:
        raw = files.read_at(parent, name, limit, path)
    except Refused as exc:
        raise _refused(exc.reason) from exc
    try:
        return ok(message=raw.decode())
    except UnicodeDecodeError as exc:
        raise _refused(f"{path} is not text, so it is not read") from exc
