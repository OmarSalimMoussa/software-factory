"""The installer's only hands on the machine. Every step acts through a Host, so a dry run says what each step
would do and changes nothing: the same step functions run against a DryHost, and for real against a RealHost
(real_host.py). What a host does goes to its journal: the owner sees phases and questions, and every command with its
output goes to the log.
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from runtime.system import qr
from runtime.system.journal import Journal


@dataclass(frozen=True, slots=True)
class Account:
    name: str
    uid: int
    gid: int
    home: Path
    groups: tuple[str, ...]
    shell: str  # the login shell passwd names


# A command whose exit code the caller reads itself: argv and its standard input, as standard/services runs them.
Runner = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class DeployKey:
    """A key the owner adds to a GitHub repo as a deploy key. The factory's read key also hands its private half over,
    once, for the product's CI to keep as an Actions secret."""

    repo: str  # owner/name of the repo the key goes on
    title: str  # the key's comment, which GitHub shows as its title
    public: str
    write: bool
    why: str  # one sentence: what the key is for
    private: str  # "" when the private half stays on this machine
    secret: str  # the Actions secret that keeps ``private``, on ``ci_repo``; "" with no private half
    ci_repo: str


class CommandError(Exception):
    """A command the installer ran failed; the message names it and ends with what it printed."""

    def __init__(self, argv: list[str], code: int, output: str) -> None:
        self.argv, self.code, self.output = argv, code, output
        super().__init__(f"{' '.join(argv)[:200]} failed with exit {code}: {output.strip()[-600:]}")


class Host(ABC):
    dry: bool
    journal: Journal

    def say(self, text: str) -> None:
        """A detail line: the log's, never the owner's main view."""
        self.journal.line(text.strip())

    def tell(self, text: str) -> None:
        """A plain sentence the owner should read."""
        self.journal.note(text)

    def phase(self, number: int, title: str) -> None:
        self.journal.phase(title)

    def step(self, number: int, title: str) -> None:
        self.journal.activity(title)

    def fail(self, problem: str) -> None:
        self.journal.failed(problem)

    def done(self) -> None:
        self.journal.summary()

    def show_code(self, secret: str, link: str) -> None:
        """The 6-digit code's secret, handed to the owner once: a QR code to scan, and the key to type instead."""
        screen = self.journal.screen
        screen("")
        screen("Scan this with your authenticator app for the 6-digit code. It is shown once, now:")
        for line in qr.half_blocks(qr.matrix(link)):
            screen(f"  {line}")
        screen(f"Or type this key into the app: {secret}")
        screen("")
        self.journal.line("the 6-digit code's secret was shown")

    def show_key(self, key: DeployKey) -> None:
        screen, kind = self.journal.screen, "a deploy key with write access" if key.write else "a read-only deploy key"
        screen("")
        screen(f"Add this key to {key.repo} on GitHub, as {kind}. {key.why}")
        screen(f"  {key.public}")
        if key.private:
            screen(f"Then save the lines below, unchanged, as {key.ci_repo}'s Actions secret {key.secret}. They are "
                   "shown once, now:")  # fmt: skip
            screen(key.private)
        screen("")
        self.journal.line(f"the key for {key.repo} was shown to add by hand")

    @abstractmethod
    def run(
        self,
        argv: list[str],
        *,
        user: str = "",
        cwd: Path | None = None,
        stdin: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> str:
        """Run a command that changes the machine; its output, or CommandError (exit 124 past ``timeout`` seconds)."""

    @abstractmethod
    def query(self, argv: list[str], *, user: str = "", cwd: Path | None = None) -> tuple[int, str]:
        """Run a command that only reads; its exit code and output."""

    @abstractmethod
    def runner(self, user: str, env: Mapping[str, str]) -> Runner:
        """Commands as ``user`` for code that reads each exit code itself; each is printed as it runs."""

    @abstractmethod
    def pipe(self, source: list[str], user: str, sink: list[str]) -> None:
        """``source``, run as ``user``, writes its bytes into ``sink``, run as root; CommandError when either fails."""

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
    def ask(self, prompt: str, *, secret: bool, hint: str, link: str) -> str:
        """The owner's answer; ``hint`` says in one line what to give, ``link`` is where it is made ("" for none)."""

    @abstractmethod
    def confirm(self, title: str, text: str, *, yes: str, no: str) -> bool:
        """Whether the owner did what ``text`` asks (``yes``), or chose ``no``."""


def as_user(account: Account, argv: list[str], env: Mapping[str, str] | None, cwd: Path | None = None) -> list[str]:
    """``argv`` run by root as ``account`` in ``cwd`` or /, with the user's home, runtime folder and session bus."""
    merged = {**user_env(account), **(env or {})}
    # -i: nothing of root's environment reaches the user's command, the install's own variables included. -C: never the
    # folder the install was started in, which may be one the user cannot enter; find -exec fails going back to it.
    return ["runuser", "-u", account.name, "--", "env", "-i", "-C", str(cwd or "/"),
            *(f"{k}={v}" for k, v in merged.items()), *argv]  # fmt: skip


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
    journal: Journal = field(default_factory=Journal)

    def _do(self, text: str) -> None:
        self.actions.append(text)
        self.say(f"would {text}")

    def run(
        self, argv: list[str], *, user: str = "", cwd: Path | None = None, stdin: str | None = None,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
    ) -> str:  # fmt: skip
        self._do(f"run: {' '.join(argv)}" + (f" (as {user})" if user else ""))
        return self.answers.get(" ".join(argv), (0, ""))[1]

    def query(self, argv: list[str], *, user: str = "", cwd: Path | None = None) -> tuple[int, str]:
        return self.answers.get(" ".join(argv), (0, ""))

    def runner(self, user: str, env: Mapping[str, str]) -> Runner:
        def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
            self._do(f"run: {' '.join(argv)} (as {user})")
            code, out = self.answers.get(" ".join(argv), (0, ""))
            return subprocess.CompletedProcess(argv, code, out, "")

        return run

    def pipe(self, source: list[str], user: str, sink: list[str]) -> None:
        self._do(f"run: {' '.join(source)} (as {user}) | {' '.join(sink)}")

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

    def ask(self, prompt: str, *, secret: bool, hint: str, link: str) -> str:
        self._do(f"ask the owner: {prompt.strip()}")
        return "<answer>"

    def confirm(self, title: str, text: str, *, yes: str, no: str) -> bool:
        self._do(f"ask the owner: {title}")
        return False
