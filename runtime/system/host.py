"""The installer's only hands on the machine. Every step acts through a Host, so a dry run prints what each step
would do and changes nothing: the same step functions run against a DryHost.

A real Host runs as root. Commands as another user go through ``runuser`` with that user's home and runtime folder,
so a user's own tools (git, uv, systemctl --user) see their own configuration.
"""

from __future__ import annotations

import getpass
import grp
import os
import pwd
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from runtime.system import layout


@dataclass(frozen=True, slots=True)
class Account:
    name: str
    uid: int
    gid: int
    home: Path
    groups: tuple[str, ...]


class CommandError(Exception):
    """A command the installer ran failed; the message names it and ends with what it printed."""

    def __init__(self, argv: list[str], code: int, output: str) -> None:
        self.argv, self.code, self.output = argv, code, output
        super().__init__(f"{' '.join(argv)[:200]} failed with exit {code}: {output.strip()[-600:]}")


class Host(ABC):
    dry: bool

    def say(self, text: str) -> None:
        print(text, flush=True)

    @abstractmethod
    def run(
        self,
        argv: list[str],
        *,
        user: str = "",
        cwd: Path | None = None,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        """Run a command that changes the machine; its output, or CommandError."""  # fmt: skip

    @abstractmethod
    def query(self, argv: list[str], *, user: str = "", cwd: Path | None = None) -> tuple[int, str]:
        """Run a command that only reads; its exit code and output."""

    @abstractmethod
    def write(self, path: Path, text: str, *, mode: int, owner: str = "root", group: str = "") -> None: ...

    @abstractmethod
    def mkdir(self, path: Path, *, mode: int, owner: str = "root", group: str = "") -> None: ...

    @abstractmethod
    def remove(self, path: Path) -> None: ...

    @abstractmethod
    def copy_tree(self, source: Path, dest: Path) -> None: ...

    @abstractmethod
    def exists(self, path: Path) -> bool: ...

    @abstractmethod
    def read(self, path: Path) -> str: ...

    @abstractmethod
    def account(self, name: str) -> Account | None: ...

    @abstractmethod
    def ask(self, prompt: str, *, secret: bool) -> str: ...

    @abstractmethod
    def scratch(self, owner: str) -> Path:
        """A new empty folder, owned by ``owner``."""


def user_env(account: Account) -> dict[str, str]:
    runtime = f"/run/user/{account.uid}"
    return {
        "HOME": str(account.home),
        "USER": account.name,
        "LOGNAME": account.name,
        "PATH": f"{account.home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": runtime,
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus",
        "LANG": "C.UTF-8",
    }


class RealHost(Host):
    dry = False

    def _argv(self, argv: list[str], user: str, env: Mapping[str, str] | None) -> list[str]:
        if not user:
            return argv if not env else ["env", *(f"{k}={v}" for k, v in env.items()), *argv]
        account = self.account(user)
        if account is None:
            raise CommandError(argv, -1, f"there is no user {user}")
        merged = {**user_env(account), **(env or {})}
        return ["runuser", "-u", user, "--", "env", *(f"{k}={v}" for k, v in merged.items()), *argv]

    def run(
        self, argv: list[str], *, user: str = "", cwd: Path | None = None, stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:  # fmt: skip
        self.say(f"  run: {' '.join(argv)[:160]}" + (f" (as {user})" if user else ""))
        done = subprocess.run(
            self._argv(argv, user, env), cwd=cwd, input=stdin, text=True, capture_output=True, check=False
        )
        if done.returncode != 0:
            raise CommandError(argv, done.returncode, done.stdout + done.stderr)
        return done.stdout

    def query(self, argv: list[str], *, user: str = "", cwd: Path | None = None) -> tuple[int, str]:
        done = subprocess.run(self._argv(argv, user, None), cwd=cwd, text=True, capture_output=True, check=False)
        return done.returncode, done.stdout + done.stderr

    def write(self, path: Path, text: str, *, mode: int, owner: str = "root", group: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.chmod(tmp, mode)
            shutil.chown(tmp, owner, group or owner)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self.say(f"  wrote {path}")

    def mkdir(self, path: Path, *, mode: int, owner: str = "root", group: str = "") -> None:
        path.mkdir(parents=True, exist_ok=True)
        shutil.chown(path, owner, group or owner)
        os.chmod(path, mode)

    def remove(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def copy_tree(self, source: Path, dest: Path) -> None:
        shutil.copytree(source, dest, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".git"))

    def exists(self, path: Path) -> bool:
        return path.exists()

    def read(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def account(self, name: str) -> Account | None:
        try:
            entry = pwd.getpwnam(name)
        # telemetry: expected — a user the installer has not created yet
        except KeyError:
            return None
        groups = tuple(g.gr_name for g in grp.getgrall() if name in g.gr_mem)
        primary = grp.getgrgid(entry.pw_gid).gr_name
        return Account(name, entry.pw_uid, entry.pw_gid, Path(entry.pw_dir), (primary, *groups))

    def ask(self, prompt: str, *, secret: bool) -> str:
        answer = getpass.getpass(prompt) if secret else input(prompt)
        return answer.strip()

    def scratch(self, owner: str) -> Path:
        folder = Path(tempfile.mkdtemp(prefix=layout.STAGE_PREFIX))
        shutil.chown(folder, owner, owner)
        return folder


@dataclass
class DryHost(Host):
    """Changes nothing and answers every read from ``facts``: what a first install on a bare machine would see,
    unless a test says otherwise."""

    users: dict[str, Account] = field(default_factory=dict)
    paths: set[Path] = field(default_factory=set)
    files: dict[Path, str] = field(default_factory=dict)
    answers: dict[str, tuple[int, str]] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    dry: bool = True

    def _do(self, text: str) -> None:
        self.actions.append(text)
        self.say(f"  would {text}")

    def run(
        self, argv: list[str], *, user: str = "", cwd: Path | None = None, stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:  # fmt: skip
        self._do(f"run: {' '.join(argv)}" + (f" (as {user})" if user else ""))
        return self.answers.get(" ".join(argv), (0, ""))[1]

    def query(self, argv: list[str], *, user: str = "", cwd: Path | None = None) -> tuple[int, str]:
        return self.answers.get(" ".join(argv), (0, ""))

    def write(self, path: Path, text: str, *, mode: int, owner: str = "root", group: str = "") -> None:
        self._do(f"write: {path} ({mode:o}, {owner}:{group or owner})")

    def mkdir(self, path: Path, *, mode: int, owner: str = "root", group: str = "") -> None:
        self._do(f"make: {path}/ ({mode:o}, {owner}:{group or owner})")

    def remove(self, path: Path) -> None:
        self._do(f"remove: {path}")

    def copy_tree(self, source: Path, dest: Path) -> None:
        self._do(f"copy: {source} to {dest}")

    def exists(self, path: Path) -> bool:
        return path in self.paths or path in self.files

    def read(self, path: Path) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def account(self, name: str) -> Account | None:
        return self.users.get(name)

    def ask(self, prompt: str, *, secret: bool) -> str:
        self._do(f"ask the owner: {prompt.strip()}")
        return "<answer>"

    def scratch(self, owner: str) -> Path:
        return Path(tempfile.gettempdir()) / f"{layout.STAGE_PREFIX}dry-run"
