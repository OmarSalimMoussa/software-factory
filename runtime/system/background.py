"""Commands an install leaves running while it goes on, such as downloading the shared services' images; the step
that needs one waits for it. Each one's command line, output and time go to the log once it is waited for."""

from __future__ import annotations

import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from runtime.system.journal import Journal


class Pending(ABC):
    """A command left running in the background; the step that needs it waits for it."""

    @abstractmethod
    def wait(self) -> tuple[int, str]:
        """Its exit code and output, once it ends."""

    @abstractmethod
    def cancel(self) -> None: ...


_ARGV_CHARS = 300  # of one command line in the log; a script passed with -c can be long


def shown(argv: list[str]) -> list[str]:
    line = " ".join(argv)
    return [line if len(line) <= _ARGV_CHARS else line[:_ARGV_CHARS] + " ..."]


class Running(Pending):
    def __init__(self, journal: Journal, argv: list[str], user: str, command: list[str], env: dict[str, str]) -> None:
        self.journal, self.argv, self.user = journal, argv, user
        # A file, not a pipe, kept open until the command is waited for: a full pipe would stall it.
        self.output = tempfile.TemporaryFile()  # noqa: SIM115
        self.began = time.monotonic()
        self.process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=self.output,
                                        stderr=subprocess.STDOUT, start_new_session=True)  # fmt: skip
        journal.line(f"started in the background: {shown(argv)[0]}" + (f" (as {user})" if user else ""))

    def wait(self) -> tuple[int, str]:
        code = self.process.wait()
        self.output.seek(0)
        said = self.output.read().decode(errors="replace")
        self.output.close()
        self.journal.command(shown(self.argv), self.user, code, said, time.monotonic() - self.began)
        return code, said

    def cancel(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            # telemetry: expected — a command that ignores the stop is killed
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.journal.line(f"stopped in the background: {shown(self.argv)[0]}")


@dataclass
class Answered(Pending):
    answer: tuple[int, str]

    def wait(self) -> tuple[int, str]:
        return self.answer

    def cancel(self) -> None:
        pass
