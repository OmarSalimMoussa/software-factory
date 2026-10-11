"""The upgrade the dashboard starts: the release's own install, run as root by the upgrade service, with nobody at
the keyboard.

A step that would ask the owner anything (a token, a key to add, a code to scan) stops the upgrade instead, saying
what it needs and to run the release file by hand to give it. Each phase and step goes into root's progress record,
which the warden reads for the dashboard while it stops and starts again around it.
"""

from __future__ import annotations

from runtime.shared import upgrades
from runtime.shared.paths import Places
from runtime.system.host import DeployKey
from runtime.system.job import refuse
from runtime.system.journal import Journal
from runtime.system.real_host import RealHost

BY_HAND = "Run the release file by hand to give it: sudo python3 software-factory.pyz"


class UnattendedHost(RealHost):
    def __init__(self, journal: Journal, places: Places, frm: str, to: str) -> None:
        super().__init__(journal)
        self.places, self.frm, self.to = places, frm, to

    def progress(self, state: str, step: str, problem: str = "") -> None:
        log = str(self.journal.path) if self.journal.path else ""
        upgrades.write_progress(self.places, self.frm, self.to, state, step, problem, log)

    def step(self, number: int, title: str) -> None:
        super().step(number, title)
        self.progress(upgrades.RUNNING, title)

    def fail(self, problem: str) -> None:
        super().fail(problem)
        self.progress(upgrades.FAILED, "", problem)

    def ask(self, prompt: str, *, secret: bool, hint: str, link: str) -> str:
        raise refuse(f"The upgrade needs something it cannot ask for here ({prompt.strip().rstrip(':')}). {BY_HAND}")

    def confirm(self, title: str, text: str, *, yes: str, no: str) -> bool:
        raise refuse(f"The upgrade needs you to confirm something ({title}). {BY_HAND}")

    def show_code(self, secret: str, link: str) -> None:
        raise refuse(f"The upgrade would make a new 6-digit code secret, which it cannot show here. {BY_HAND}")

    def show_key(self, key: DeployKey) -> None:
        raise refuse(f"The upgrade needs a key added to {key.repo} by hand. {BY_HAND}")
