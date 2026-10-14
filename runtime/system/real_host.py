"""The Host that changes the machine. It runs as root; its commands as root run with the one fixed PATH; commands as
another user go through ``runuser`` with nothing but that user's home, runtime folder and session bus, so a user's own
tools (git, uv, systemctl --user) see their own configuration and nothing of the installer's.
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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from runtime.shared import environment
from runtime.system import layout
from runtime.system.background import Pending, Running, shown
from runtime.system.host import Account, CommandError, Host, Runner, as_user
from runtime.system.journal import Journal


class RealHost(Host):
    dry = False

    def __init__(self, journal: Journal | None = None) -> None:
        self.journal = journal or Journal()

    def _spawn(
        self, argv: list[str], user: str, env: Mapping[str, str] | None, cwd: Path | None = None
    ) -> tuple[list[str], dict[str, str]]:
        """The command line and the environment it starts with: as root, this process's own with the fixed PATH; as
        another user, ``runuser`` starts it with only what ``as_user`` declares."""
        root = {**environment.handed_on(), "PATH": layout.ROOT_PATH}
        if not user:
            return argv, {**root, **(env or {})}
        account = self.account(user)
        if account is None:
            raise CommandError(argv, -1, f"there is no user {user}")
        return as_user(account, argv, env, cwd), root

    @staticmethod
    def _started(command: list[str], **how: Any) -> subprocess.CompletedProcess[str]:
        """``command`` run to its end; a program that is not there ends as the shell's 127 would, never raises."""
        try:
            done: subprocess.CompletedProcess[str] = subprocess.run(command, text=True, capture_output=True,
                                                                    check=False, **how)  # fmt: skip
        # telemetry: surfaced — a program that is not there comes back as a failed `CompletedProcess`
        except FileNotFoundError as exc:
            return subprocess.CompletedProcess(command, 127, "", f"cannot start {command[0]}: {exc.strerror}")
        return done

    def run(
        self, argv: list[str], *, user: str = "", cwd: Path | None = None, stdin: str | None = None,
        env: Mapping[str, str] | None = None, timeout: float | None = None,
    ) -> str:  # fmt: skip
        command, child_env = self._spawn(argv, user, env, cwd)
        began = time.monotonic()
        # Without input of its own, a command reads nothing: never the owner's terminal.
        feed: dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        try:
            done = self._started(command, cwd=cwd, env=child_env, timeout=timeout, **feed)
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
            command, child_env = self._spawn(argv, user, None, cwd)
        # telemetry: expected — a user the install has not made yet, as a dry run on a new machine finds
        except CommandError as exc:
            self.journal.command(shown(argv), user, exc.code, exc.output, 0)
            return exc.code, exc.output
        began = time.monotonic()
        done = self._started(command, cwd=cwd, env=child_env, stdin=subprocess.DEVNULL)
        self.journal.command(shown(argv), user, done.returncode, done.stdout + done.stderr, time.monotonic() - began)
        return done.returncode, done.stdout + done.stderr

    def runner(self, user: str, env: Mapping[str, str]) -> Runner:
        def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
            feed: dict[str, Any] = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
            command, child_env = self._spawn(argv, user, env)
            began = time.monotonic()
            done = self._started(command, env=child_env, **feed)
            # What these commands print is the shared services' keys and tokens: the log keeps only how they ended.
            self.journal.command(shown(argv), user, done.returncode, "(output not kept: it holds keys)",
                                 time.monotonic() - began)  # fmt: skip
            return subprocess.CompletedProcess(argv, done.returncode, done.stdout, done.stderr)

        return run

    def background(self, argv: list[str], *, user: str, env: Mapping[str, str]) -> Pending:
        command, child_env = self._spawn(argv, user, env)
        return Running(self.journal, argv, user, command, child_env)

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
