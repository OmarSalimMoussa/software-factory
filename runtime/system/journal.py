"""What an install or an uninstall shows the owner, and the log that keeps every detail.

The terminal shows a few clean lines: each phase with a check mark once it is done (on a terminal, the running phase's
line also names what it is doing), the questions, a note when there is one, and, when a run stops, one plain sentence
and where the log is. Every detail line goes to the log: each command, its output and how long it took, and how long
each step and phase took. ``--verbose`` prints the detail lines too. No secret is ever written: an answer, a command's
input and a shown secret stay off the log.
"""

from __future__ import annotations

import grp
import os
import shutil
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

_OUTPUT_CHARS = 20_000  # of one command's output, kept from its end
DONE, FAILED, RUNNING = "✓", "✗", "…"


class Journal:
    def __init__(self, path: Path | None = None, *, verbose: bool = False, terminal: bool = True) -> None:
        self.path = path  # None: the details are kept nowhere but, with verbose, the terminal
        self.verbose = verbose
        self.terminal = terminal  # False: nothing is printed, as when root's upgrade unit runs it
        self._lock = threading.RLock()
        self._began = time.monotonic()
        self._phase: tuple[str, float] | None = None
        self._step: tuple[str, float] | None = None
        self._open = False  # a running phase's line is on the terminal without its line end
        self.timings: list[tuple[str, float]] = []  # each phase and how long it took

    @classmethod
    def create(cls, folder: Path, kind: str, *, group: str, verbose: bool, terminal: bool = True) -> Journal:
        """A new log in ``folder``, which only root writes and the owner's ``group`` reads; "" for no group."""
        folder.mkdir(mode=0o750, parents=True, exist_ok=True)
        gid = grp.getgrnam(group).gr_gid if group else -1
        if group:
            os.chown(folder, 0, gid)
        os.chmod(folder, 0o750)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        for n in range(1, 100):
            path = folder / f"{kind}-{stamp}{'' if n == 1 else f'-{n}'}.log"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o640)
            # telemetry: expected — another run began in the same second; the next name is tried
            except FileExistsError:
                continue
            if group:
                os.fchown(fd, 0, gid)
            os.fchmod(fd, 0o640)
            os.close(fd)
            made = cls(path, verbose=verbose, terminal=terminal)
            made._write(f"{kind} began {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}; times are seconds since")
            return made
        raise FileExistsError(f"{folder} holds a log for every name of {stamp}")

    # ---- detail: the log, and the terminal only with verbose ---------------------------------------------------

    def line(self, text: str) -> None:
        with self._lock:
            self._write(text)
            if self.verbose:
                self._print(f"    {text.strip()}" if text.strip() else "")

    def command(self, argv: list[str], user: str, code: int, output: str, seconds: float) -> None:
        who = f" (as {user})" if user else ""
        lines = [f"$ {' '.join(argv)}{who}"]
        kept = output.strip()[-_OUTPUT_CHARS:]
        lines += [f"  | {each}" for each in kept.splitlines()]
        lines.append(f"  exit {code} after {seconds:.1f} s")
        with self._lock:
            for each in lines:
                self.line(each)

    def _write(self, text: str) -> None:
        if self.path is None:
            return
        at = time.monotonic() - self._began
        with self.path.open("a", encoding="utf-8") as fh:
            for each in text.splitlines() or [""]:
                fh.write(f"{at:8.1f}  {each}\n")

    # ---- the terminal ------------------------------------------------------------------------------------------

    def _print(self, text: str) -> None:
        if not self.terminal:
            return
        if self._open:
            sys.stdout.write("\n")
            self._open = False
        print(text, flush=True)

    def screen(self, text: str) -> None:
        """A line for the owner: a question's words, a key to add, a note."""
        with self._lock:
            self._print(text)

    def pause(self) -> None:
        """Ends the running phase's line, so what the terminal prints next starts a line of its own."""
        with self._lock:
            if self._open:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._open = False

    def note(self, text: str) -> None:
        with self._lock:
            self._write(f"note: {text}")
            self._print(f"  {text}")

    def phase(self, title: str) -> None:
        with self._lock:
            self.end_phase()
            self._phase = (title, time.monotonic())
            self._write(f"== {title}")
            self._show(title)

    def activity(self, text: str) -> None:
        with self._lock:
            self._end_step()
            self._step = (text, time.monotonic())
            self._write(f"-- {text}")
            if self._phase is not None and text != self._phase[0]:
                self._show(f"{self._phase[0]}: {text}")

    def _show(self, text: str) -> None:
        """The running phase's line: rewritten in place on a terminal; on anything else, printed when it ends."""
        if self.verbose:
            self._print(f"  {RUNNING} {text}")
        elif self.terminal and sys.stdout.isatty():
            width = shutil.get_terminal_size().columns - 1
            sys.stdout.write(f"\r\x1b[K  {RUNNING} {text}"[: width + 4])
            sys.stdout.flush()
            self._open = True

    def _end_step(self) -> None:
        if self._step is not None:
            self._write(f"   took {time.monotonic() - self._step[1]:.1f} s")
            self._step = None

    def end_phase(self, *, failed: bool = False) -> None:
        with self._lock:
            self._end_step()
            if self._phase is None:
                return
            title, began = self._phase
            took = time.monotonic() - began
            self.timings.append((title, took))
            self._write(f"== {title}: {'failed' if failed else 'done'} after {took:.1f} s")
            if self._open:
                sys.stdout.write("\r\x1b[K")
                self._open = False
            if self.terminal:
                print(f"  {FAILED if failed else DONE} {title}", flush=True)
            self._phase = None

    def failed(self, sentence: str) -> None:
        with self._lock:
            self.end_phase(failed=True)
            self._write(f"stopped: {sentence}")
            self._print("")
            self._print(sentence)

    def summary(self) -> None:
        """How long each phase took, written last."""
        with self._lock:
            self.end_phase()
            self._write("phase times: " + "; ".join(f"{title} {took:.0f} s" for title, took in self.timings))

    def where(self) -> str:
        return f"Every detail is in the log: {self.path}" if self.path else ""
