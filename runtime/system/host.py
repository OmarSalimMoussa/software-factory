"""The installer's only hands on the machine. Every step acts through a Host, so a dry run says what each step
would do and changes nothing: the same step functions run against a DryHost.

A real Host runs as root. Its commands as root run with the one fixed PATH; commands as another user go through
``runuser`` with nothing but that user's home, runtime folder and session bus, so a user's own tools (git, uv,
systemctl --user) see their own configuration and nothing of the installer's. What a host does goes to its journal:
the owner sees phases and questions, and every command with its output goes to the log.
"""

from __future__ import annotations

import getpass
import grp
import os
import pwd
import shutil
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.system import layout, qr
from runtime.system.background import Answered, Pending, Running, shown
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

    def show_folder(self, label: str, folder: Path) -> None:
        self.journal.line(f"{label}: {folder}")

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
    def background(self, argv: list[str], *, user: str, env: Mapping[str, str]) -> Pending:
        """Start a command as ``user`` and return at once; its output goes to the log once it is waited for."""

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


def as_user(account: Account, argv: list[str], env: Mapping[str, str] | None) -> list[str]:
    """``argv`` run by root as ``account``, with that user's home, runtime folder and session bus."""
    merged = {**user_env(account), **(env or {})}
    # -i: nothing of root's environment reaches the user's command, the install's own variables included.
    return ["runuser", "-u", account.name, "--", "env", "-i", *(f"{k}={v}" for k, v in merged.items()), *argv]


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

    def __init__(self, journal: Journal | None = None) -> None:
        self.journal = journal or Journal()

    def _spawn(self, argv: list[str], user: str, env: Mapping[str, str] | None) -> tuple[list[str], dict[str, str]]:
        """The command line and the environment it starts with: as root, this process's own with the fixed PATH; as
        another user, ``runuser`` starts it with only what ``as_user`` declares."""
        root = {**os.environ, "PATH": layout.ROOT_PATH}
        if not user:
            return argv, {**root, **(env or {})}
        account = self.account(user)
        if account is None:
            raise CommandError(argv, -1, f"there is no user {user}")
        return as_user(account, argv, env), root

    @staticmethod
    def _started(command: list[str], **how: Any) -> subprocess.CompletedProcess[str]:
        """``command`` run to its end; a program that is not there ends as the shell's 127 would, never raises."""
        try:
            done: subprocess.CompletedProcess[str] = subprocess.run(command, text=True, capture_output=True,
                                                                    check=False, **how)  # fmt: skip
        except FileNotFoundError as exc:
            return subprocess.CompletedProcess(command, 127, "", f"cannot start {command[0]}: {exc.strerror}")
        return done

    def run(
        self, argv: list[str], *, user: str = "", cwd: Path | None = None, stdin: str | None = None,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
    ) -> str:  # fmt: skip
        command, environ = self._spawn(argv, user, env)
        began = time.monotonic()
        # Without input of its own, a command reads nothing: never the owner's terminal.
        feed: dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        try:
            done = self._started(command, cwd=cwd, env=environ, timeout=timeout, **feed)
        except subprocess.TimeoutExpired as exc:
            said = "".join(part if isinstance(part, str) else part.decode(errors="replace")
                           for part in (exc.stdout, exc.stderr) if part)  # fmt: skip
            self.journal.command(shown(argv), user, 124, said + f"\n(stopped after {timeout} s)", timeout or 0)
            raise CommandError(argv, 124, said) from exc
        self.journal.command(shown(argv), user, done.returncode, done.stdout + done.stderr, time.monotonic() - began)
        if done.returncode != 0:
            raise CommandError(argv, done.returncode, done.stdout + done.stderr)
        return str(done.stdout)

    def query(self, argv: list[str], *, user: str = "", cwd: Path | None = None) -> tuple[int, str]:
        try:
            command, environ = self._spawn(argv, user, None)
        # telemetry: expected — a user the install has not made yet, as a dry run on a new machine finds
        except CommandError as exc:
            self.journal.command(shown(argv), user, exc.code, exc.output, 0)
            return exc.code, exc.output
        began = time.monotonic()
        done = self._started(command, cwd=cwd, env=environ, stdin=subprocess.DEVNULL)
        self.journal.command(shown(argv), user, done.returncode, done.stdout + done.stderr, time.monotonic() - began)
        return done.returncode, done.stdout + done.stderr

    def runner(self, user: str, env: Mapping[str, str]) -> Runner:
        def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
            feed: dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
            command, environ = self._spawn(argv, user, env)
            began = time.monotonic()
            done = self._started(command, env=environ, **feed)
            # What these commands print is the shared services' keys and tokens: the log keeps only how they ended.
            self.journal.command(shown(argv), user, done.returncode, "(output not kept: it holds keys)",
                                 time.monotonic() - began)  # fmt: skip
            return subprocess.CompletedProcess(argv, done.returncode, done.stdout, done.stderr)

        return run

    def background(self, argv: list[str], *, user: str, env: Mapping[str, str]) -> Pending:
        command, environ = self._spawn(argv, user, env)
        return Running(self.journal, argv, user, command, environ)

    def pipe(self, source: list[str], user: str, sink: list[str]) -> None:
        made, made_env = self._spawn(source, user, None)
        taken, taken_env = self._spawn(sink, "", None)
        began = time.monotonic()
        # The source's errors go to a file, not a pipe: a full pipe would stall it while the sink waits for its end.
        with tempfile.TemporaryFile() as errors:
            producer = subprocess.Popen(made, env=made_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=errors)  # fmt: skip
            try:
                done = subprocess.run(taken, env=taken_env, stdin=producer.stdout, capture_output=True, check=False)
            finally:
                if producer.stdout is not None:
                    producer.stdout.close()
                code = producer.wait()
            errors.seek(0)
            said = (errors.read() + done.stdout + done.stderr).decode(errors="replace")
        self.journal.command([*shown(source), "|", *shown(sink)], user, done.returncode or code, said,
                             time.monotonic() - began)  # fmt: skip
        if done.returncode != 0:
            raise CommandError(sink, done.returncode, said)
        if code != 0:
            raise CommandError(source, code, said)

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
        self.say(f"wrote {path} ({mode:o}, {owner}:{group or owner})")

    def mkdir(self, path: Path, *, mode: int, owner: str = "root", group: str = "") -> None:
        path.mkdir(parents=True, exist_ok=True)
        shutil.chown(path, owner, group or owner)
        os.chmod(path, mode)

    def remove(self, path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        self.say(f"removed {path}")

    def copy_tree(self, source: Path, dest: Path) -> None:
        began = time.monotonic()
        shutil.copytree(source, dest, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        self.say(f"copied {source} to {dest} in {time.monotonic() - began:.1f} s")

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
        # An empty shell field means /bin/sh, as login(1) reads it.
        shell = entry.pw_shell or "/bin/sh"
        return Account(name, entry.pw_uid, entry.pw_gid, Path(entry.pw_dir), (primary, *groups), shell)

    def ask(self, prompt: str, *, secret: bool, hint: str, link: str) -> str:
        self.journal.screen("")
        for line in (hint, link):
            if line:
                self.journal.screen(line)
        self.journal.line(f"asked: {prompt.strip()}")
        answer = getpass.getpass(prompt) if secret else input(prompt)
        return answer.strip()

    def confirm(self, title: str, text: str, *, yes: str, no: str) -> bool:
        self.journal.screen("")
        self.journal.screen(f"{title}. {text}")
        self.journal.line(f"asked: {title}")
        answer = input(f"Press Enter once you have ({yes}), or type no ({no}): ")
        return answer.strip().lower() not in ("n", "no")


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

    def background(self, argv: list[str], *, user: str, env: Mapping[str, str]) -> Pending:
        self._do(f"start in the background: {' '.join(argv)} (as {user})")
        return Answered(self.answers.get(" ".join(argv), (0, "")))

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
