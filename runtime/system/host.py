"""The installer's only hands on the machine. Every step acts through a Host, so a dry run prints what each step
would do and changes nothing: the same step functions run against a DryHost.

A real Host runs as root. Its commands as root run with the one fixed PATH; commands as another user go through
``runuser`` with nothing but that user's home, runtime folder and session bus, so a user's own tools (git, uv,
systemctl --user) see their own configuration and nothing of the installer's.
"""

from __future__ import annotations

import getpass
import grp
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.system import layout


@dataclass(frozen=True, slots=True)
class Account:
    name: str
    uid: int
    gid: int
    home: Path
    groups: tuple[str, ...]


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

    def say(self, text: str) -> None:
        print(text, flush=True)

    # What the owner sees of a run beyond its lines: on the command line, more lines; the console draws each its way.

    def step(self, number: int, title: str) -> None:
        self.say(f"{number:>2}. {title}")

    def fail(self, program: str, problem: str) -> None:
        print(f"{program}: {problem}", file=sys.stderr, flush=True)

    def show_code(self, secret: str, link: str) -> None:
        """The 6-digit code's secret, handed to the owner once."""
        self.say("  Add this to your authenticator app. It is shown once, now:")
        self.say(f"    {link}")

    def show_key(self, key: DeployKey) -> None:
        access = "with write access" if key.write else "read-only"
        self.say(f"  Add this as a deploy key to {key.repo}, {access}. {key.why}")
        self.say(f"    {key.public}")
        if key.private:
            self.say(f"  Save the lines below, unchanged, as {key.ci_repo}'s secret {key.secret}. Shown once, now:")
            self.say(key.private)

    def show_folder(self, label: str, folder: Path) -> None:
        self.say(f"  {label}: {folder}")

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

    def run(
        self, argv: list[str], *, user: str = "", cwd: Path | None = None, stdin: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:  # fmt: skip
        self.say(f"  run: {' '.join(argv)[:160]}" + (f" (as {user})" if user else ""))
        command, environ = self._spawn(argv, user, env)
        done = subprocess.run(command, cwd=cwd, input=stdin, text=True, capture_output=True, check=False, env=environ)
        if done.returncode != 0:
            raise CommandError(argv, done.returncode, done.stdout + done.stderr)
        return done.stdout

    def query(self, argv: list[str], *, user: str = "", cwd: Path | None = None) -> tuple[int, str]:
        command, environ = self._spawn(argv, user, None)
        done = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False, env=environ)
        return done.returncode, done.stdout + done.stderr

    def runner(self, user: str, env: Mapping[str, str]) -> Runner:
        def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
            self.say(f"  run: {' '.join(argv)[:160]} (as {user})")
            # Without input of its own, a command reads nothing: never the owner's terminal.
            feed: dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
            command, environ = self._spawn(argv, user, env)
            done = subprocess.run(command, text=True, capture_output=True, check=False, env=environ, **feed)
            return subprocess.CompletedProcess(argv, done.returncode, done.stdout, done.stderr)

        return run

    def pipe(self, source: list[str], user: str, sink: list[str]) -> None:
        self.say(f"  run: {' '.join(source)[:120]} (as {user}) | {' '.join(sink)[:120]}")
        made, made_env = self._spawn(source, user, None)
        taken, taken_env = self._spawn(sink, "", None)
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

    def ask(self, prompt: str, *, secret: bool, hint: str, link: str) -> str:
        for line in (hint, link):
            if line:
                self.say(f"  {line}")
        answer = getpass.getpass(prompt) if secret else input(prompt)
        return answer.strip()


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
