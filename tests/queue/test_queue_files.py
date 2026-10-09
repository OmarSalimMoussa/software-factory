"""The warden never follows a link out of the queue, and never writes into a queue folder others can write."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from runtime.queue.files import QueueFiles
from runtime.shared.protocol import Refused


def _symlinked_queue(repo: Path, elsewhere: Path) -> int:
    (repo / "queue").rmdir()
    (repo / "queue").symlink_to(elsewhere)
    return os.geteuid()


def _foreign_owner(repo: Path, elsewhere: Path) -> int:
    return os.geteuid() + 1


def _writable_by_others(repo: Path, elsewhere: Path) -> int:
    (repo / "queue").chmod(0o777)
    return os.geteuid()


def _symlinked_folder(repo: Path, elsewhere: Path) -> int:
    (repo / "queue" / "tasks").symlink_to(elsewhere)
    return os.geteuid()


@pytest.mark.parametrize("setup", [_symlinked_queue, _foreign_owner, _writable_by_others, _symlinked_folder])
def test_an_unsafe_queue_is_refused_and_nothing_lands_elsewhere(
    tmp_path: Path, setup: Callable[[Path, Path], int]
) -> None:
    repo, elsewhere = tmp_path / "repo", tmp_path / "warden-state"
    (repo / "queue").mkdir(parents=True)
    (repo / "queue").chmod(0o755)
    elsewhere.mkdir()
    files = QueueFiles(repo, setup(repo, elsewhere))
    with pytest.raises(Refused) as refused:
        files.write("tasks/t-0001.md", b"x")
    assert refused.value.rule == "queue-unsafe"
    assert list(elsewhere.iterdir()) == []


def test_a_symlinked_file_is_never_read(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "queue" / "tasks").mkdir(parents=True)
    for folder in (repo / "queue", repo / "queue" / "tasks"):
        folder.chmod(0o755)
    (tmp_path / "secret").write_text("key")
    (repo / "queue" / "tasks" / "t-0001.md").symlink_to(tmp_path / "secret")
    with pytest.raises(Refused, match="symlink"):
        QueueFiles(repo, os.geteuid()).read("tasks/t-0001.md")


def test_a_batch_with_one_bad_file_writes_none(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "queue").mkdir(parents=True)
    (repo / "queue").chmod(0o755)
    files = QueueFiles(repo, os.geteuid())
    with pytest.raises(Refused):
        files.write_many({"tasks/t-0001.md": b"one", "tasks/../escape.md": b"two"})
    assert files.names("tasks") == []
    assert not any(p.name.endswith(".tmp") for p in (repo / "queue").rglob("*"))
