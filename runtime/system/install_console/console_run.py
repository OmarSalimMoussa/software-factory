"""One action the install console runs, and the hosts it runs with.

A run holds what the page draws: its phases, the running phase's one-line activity, how far it has come, the question
the owner is answering, and how it ended. Never a command or its output: those go to the run's log. The worker thread
writes the run and the page's requests read and answer it, so every access holds its lock. A question blocks its step
until the page answers; an answer goes straight to that step and is never kept or drawn again.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime.system import qr
from runtime.system.host import Account, DeployKey, DryHost, Host
from runtime.system.job import refuse
from runtime.system.journal import Journal
from runtime.system.real_host import RealHost
from runtime.system.uninstall import LookingHost

WAITING, RUNNING, DONE, FAILED = "waiting", "running", "done", "failed"
STATE_TEXT = {WAITING: "Waiting", RUNNING: "Running", DONE: "Done", FAILED: "Failed"}
# The mark the page draws beside a phase: an icon, or its own spinner or empty ring.
STATE_MARK = {WAITING: "ring", RUNNING: "spin", DONE: "check", FAILED: "x-mark"}


@dataclass
class Row:
    title: str
    state: str = WAITING


class Run:
    """``phases`` are drawn from the start; ``steps`` is how many steps they hold, for the progress bar. A phase no
    row foresaw, such as a failed install's roll-back, gets a row of its own."""

    def __init__(self, kind: str, title: str, dry: bool, phases: tuple[str, ...], steps: int, owner: Account,
                 journal: Journal) -> None:  # fmt: skip
        self.kind, self.title, self.dry, self.owner, self.journal = kind, title, dry, owner, journal
        self.rows = [Row(name) for name in phases]
        self.steps, self.finished_steps, self.stepping = steps, 0, False
        self.activity = ""
        self.notes: list[str] = []  # plain sentences a step had for the owner
        self.cond = threading.Condition()
        self.current: int | None = None
        self.broke = False  # a step failed: what runs after it is not progress
        self.question: dict[str, Any] | None = None
        self.answer: tuple[str, str] | None = None
        self.asked = 0
        self.closed = False
        self.folder: Path | None = None  # a folder a step named for the owner to read, such as the drills' output
        self.ending: dict[str, Any] | None = None

    # ---- the worker's side ---------------------------------------------------------------------------------------

    def note(self, text: str) -> None:
        with self.cond:
            self.notes.append(text)

    def phase(self, title: str) -> None:
        with self.cond:
            self._settle(DONE)
            start = 0 if self.current is None else self.current + 1
            found = next((n for n in range(start, len(self.rows))
                          if self.rows[n].state == WAITING and self.rows[n].title == title), None)  # fmt: skip
            if found is None:
                self.rows.append(Row(title))
                found = len(self.rows) - 1
            self.rows[found].state = RUNNING
            self.current = found

    def step(self, title: str) -> None:
        with self.cond:
            if self.stepping and not self.broke:
                self.finished_steps += 1
            self.stepping, self.activity = True, title

    def fail(self) -> None:
        """The step running failed; what runs after it, such as a roll-back, is not the action's progress."""
        with self.cond:
            self._settle(FAILED)
            self.current, self.broke, self.stepping, self.activity = None, True, False, ""

    def finish(self, ending: dict[str, Any], *, broke: bool) -> None:
        with self.cond:
            if not broke and not self.broke and self.stepping:
                self.finished_steps += 1
            self._settle(FAILED if broke else DONE)
            self.current, self.activity = None, ""
            self.ending = ending

    def _settle(self, state: str) -> None:
        if self.current is not None and self.rows[self.current].state == RUNNING:
            self.rows[self.current].state = state

    def ask(self, question: dict[str, Any]) -> tuple[str, str]:
        """Shows ``question`` and waits for the page's answer: the action pressed and the field's value."""
        with self.cond:
            self.asked += 1
            self.question = {**question, "id": f"q{self.asked}"}
            self.answer = None
            while self.answer is None and not self.closed:
                self.cond.wait()
            self.question = None
            answer, self.answer = self.answer, None
        if answer is None:
            raise refuse("The console was stopped while it waited for your answer: start it again to finish")
        return answer

    # ---- the page's side -----------------------------------------------------------------------------------------

    def respond(self, question: str, action: str, value: str) -> str | None:
        """None once the answer is handed to the step waiting for it; else why it was not."""
        with self.cond:
            asked = self.question
            if asked is None or asked["id"] != question:
                return "That question was answered already."
            if action not in {a["id"] for a in asked["actions"]}:
                return "That is not one of the question's answers."
            self.answer = (action, value)
            self.cond.notify_all()
            return None

    def close(self) -> None:
        """The console is stopping: a step waiting for an answer, or asking later, is refused."""
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    @property
    def finished(self) -> bool:
        with self.cond:
            return self.ending is not None

    def view(self) -> dict[str, Any]:
        with self.cond:
            phases = [
                {"title": r.title, "state": r.state, "state_text": STATE_TEXT[r.state], "mark": STATE_MARK[r.state]}
                for r in self.rows
            ]
            if self.ending is None:
                chip = {"text": "Running", "tone": "accent"}
            else:
                chip = {"text": "Finished", "tone": "good"} if self.ending["ok"] else {"text": "Stopped", "tone": "bad"}
            return {
                "id": f"{id(self):x}",
                "kind": self.kind,
                "title": self.title,
                "chip": chip,
                "badge": "Changes nothing" if self.dry else "",
                "phases": phases,
                "activity": self.activity,
                "progress": round(100 * min(self.finished_steps, self.steps) / self.steps) if self.steps else 100,
                "notes": list(self.notes),
                "log": "Show the log" if self.journal.path else "",
                "question": copy.deepcopy(self.question),
                "ending": copy.deepcopy(self.ending),
            }


# ---- the questions, as the page draws them -----------------------------------------------------------------------


def _action(ident: str, label: str, tone: str) -> dict[str, str]:
    return {"id": ident, "label": label, "tone": tone}


def _box(label: str, value: str) -> dict[str, str]:
    return {"label": label, "value": value, "copy": "Copy", "copied": "Copied"}


def _question(title: str, text: str, actions: list[dict[str, str]], *, boxes: list[dict[str, str]] | None = None,
              field: dict[str, str] | None = None, links: list[dict[str, str]] | None = None,
              code: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    return {"title": title, "text": text, "qr": code, "boxes": boxes or [], "field": field, "links": links or [],
            "actions": actions, "note": ""}  # fmt: skip


def asked(prompt: str, *, secret: bool, hint: str, link: str) -> dict[str, Any]:
    title = prompt.strip().removesuffix(":").strip()
    field = {"type": "password" if secret else "text"}
    return _question(title, hint, [_action("send", "Send", "primary")], field=field,
                     links=[{"label": "How to make it", "url": link}] if link else [])  # fmt: skip


def code_question(secret: str, link: str) -> dict[str, Any]:
    """The 6-digit code's secret as a QR code to scan, its key below it to type instead."""
    grid = qr.matrix(link)
    text = ("Scan this with your authenticator app. It is shown once, now. The dashboard asks for a code from it to "
            "sign in a device and before each coded action, such as Push to prod.")  # fmt: skip
    return _question(
        "Add the 6-digit code to your authenticator app", text, [_action("done", "I added it", "primary")],
        boxes=[_box("Or type this key into the app", secret)],
        links=[{"label": "Open it in your authenticator app", "url": link}] if link.startswith("otpauth://") else [],
        code={"size": len(grid) + 2 * qr.QUIET, "path": qr.svg_path(grid), "label": "The 6-digit code's QR code"},
    )  # fmt: skip


def key_question(key: DeployKey) -> dict[str, Any]:
    """A key the owner adds on GitHub, with where to add it; for the read key, its private half as a secret too."""
    access = "with write access" if key.write else "read-only"
    text = f"{key.why} Add it under the repository's Settings, Deploy keys, {access}."
    boxes, links = [_box("The key", key.public)], [{"label": "Add it on GitHub", "url":
                                                     f"https://github.com/{key.repo}/settings/keys/new"}]  # fmt: skip
    if key.private:
        text += f" Then save the second box, unchanged, as {key.ci_repo}'s Actions secret {key.secret}. Shown once."
        boxes.append(_box(f"The secret {key.secret}", key.private))
        links.append({"label": "Add the secret on GitHub",
                      "url": f"https://github.com/{key.ci_repo}/settings/secrets/actions/new"})  # fmt: skip
    return _question(f"Add a deploy key to {key.repo}", text, [_action("done", "I added it", "primary")], boxes=boxes,
                     links=links)  # fmt: skip


def confirm_question(title: str, text: str, yes: str, no: str) -> dict[str, Any]:
    return _question(title, text, [_action("yes", yes, "primary"), _action("no", no, "")])


# ---- the hosts a run uses ----------------------------------------------------------------------------------------


class Paged(Host):
    """A host whose phases, notes and questions go to the page instead of the terminal, and whose every detail goes
    to the run's log. It comes first in a console host's bases, so the host after it does every other thing."""

    page: Run

    def tell(self, text: str) -> None:
        self.journal.note(text)
        self.page.note(text)

    def phase(self, number: int, title: str) -> None:
        self.journal.phase(title)
        self.page.phase(title)

    def step(self, number: int, title: str) -> None:
        self.journal.activity(title)
        self.page.step(title)

    def fail(self, problem: str) -> None:
        self.journal.failed(problem)
        self.page.fail()

    def ask(self, prompt: str, *, secret: bool, hint: str, link: str) -> str:
        self.journal.line(f"asked: {prompt.strip()}")
        _, value = self.page.ask(asked(prompt, secret=secret, hint=hint, link=link))
        return value.strip()

    def confirm(self, title: str, text: str, *, yes: str, no: str) -> bool:
        self.journal.line(f"asked: {title}")
        action, _ = self.page.ask(confirm_question(title, text, yes, no))
        return action == "yes"

    def show_code(self, secret: str, link: str) -> None:
        self.page.ask(code_question(secret, link))
        self.journal.line("the 6-digit code's secret was shown")

    def show_key(self, key: DeployKey) -> None:
        self.page.ask(key_question(key))
        self.journal.line(f"the key for {key.repo} was shown to add by hand")

    def show_folder(self, label: str, folder: Path) -> None:
        self.page.folder = folder
        self.journal.line(f"{label}: {folder}")


class ConsoleHost(Paged, RealHost):
    """Changes the machine, as root, like the command line's host."""

    def __init__(self, page: Run) -> None:
        RealHost.__init__(self, page.journal)
        self.page = page


class ConsoleLookingHost(Paged, LookingHost):
    """The console's dry run: reads this machine for real and changes nothing."""

    def __init__(self, page: Run) -> None:
        LookingHost.__init__(self, page.journal)
        self.page = page


class ConsoleDryHost(Paged, DryHost):
    """Changes nothing and reads only the facts given: what a test drives."""

    def __init__(self, page: Run, answers: dict[str, tuple[int, str]]) -> None:
        DryHost.__init__(self, answers=answers, journal=page.journal)
        self.page = page
