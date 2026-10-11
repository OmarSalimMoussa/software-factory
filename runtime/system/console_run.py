"""One action the install console runs, and the hosts it runs with.

A run holds what the page draws: a row for each step with the lines it said, the question the owner is answering,
and how it ended. The worker thread writes it and the page's requests read and answer it, so every access holds its
lock. A question blocks its step until the page answers; an answer goes straight to that step and is never kept or
drawn again.
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime.system.host import Account, CommandError, DeployKey, DryHost, Host, RealHost
from runtime.system.job import refuse
from runtime.system.uninstall import LookingHost

WAITING, RUNNING, DONE, FAILED = "waiting", "running", "done", "failed"
STATE_TEXT = {WAITING: "Waiting", RUNNING: "Running", DONE: "Done", FAILED: "Failed"}
_GITHUB = "https://github.com"
_SHOWN = 300  # how much of a command's output a refused GitHub call shows


@dataclass
class Row:
    title: str
    group: str  # "" for the action's own steps; a name for steps run after a failure, such as a roll-back
    state: str = WAITING
    lines: list[str] = field(default_factory=list)


class Run:
    """``after_failure`` names the group of steps that run after a step fails: a first install's roll-back."""

    def __init__(self, kind: str, title: str, dry: bool, steps: tuple[str, ...], owner: Account,
                 after_failure: str) -> None:  # fmt: skip
        self.kind, self.title, self.dry, self.owner, self.after_failure = kind, title, dry, owner, after_failure
        self.rows = [Row(name, "") for name in steps]
        self.notes: list[str] = []  # what was said while no step ran
        self.cond = threading.Condition()
        self.current: int | None = None
        self.group = ""
        self.question: dict[str, Any] | None = None
        self.answer: tuple[str, str] | None = None
        self.asked = 0
        self.closed = False
        self.folder: Path | None = None  # a folder a step named for the owner to read, such as the drills' output
        self.ending: dict[str, Any] | None = None

    # ---- the worker's side ---------------------------------------------------------------------------------------

    def line(self, text: str) -> None:
        with self.cond:
            (self.rows[self.current].lines if self.current is not None else self.notes).append(text)

    def step(self, title: str) -> None:
        """The next row with this title runs; a step no row foresaw, such as a roll-back's, gets a row of its own."""
        with self.cond:
            self._settle(DONE)
            start = 0 if self.current is None else self.current + 1
            found = next((n for n in range(start, len(self.rows)) if self.rows[n].state == WAITING
                          and self.rows[n].title == title and self.rows[n].group == self.group), None)  # fmt: skip
            if found is None:
                self.rows.append(Row(title, self.group))
                found = len(self.rows) - 1
            self.rows[found].state = RUNNING
            self.current = found

    def fail(self) -> None:
        """The step running failed; any step after it belongs to what follows a failure."""
        with self.cond:
            self._settle(FAILED)
            self.current = None
            self.group = self.after_failure

    def finish(self, ending: dict[str, Any], *, broke: bool) -> None:
        with self.cond:
            self._settle(FAILED if broke else DONE)
            self.current = None
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
            raise refuse("the console was stopped while it waited for your answer")
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
            rows = [{"title": r.title, "group": r.group, "state": r.state, "state_text": STATE_TEXT[r.state],
                     "lines": list(r.lines)} for r in self.rows]  # fmt: skip
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
                "open": self.kind == "look",  # what an uninstall would remove is shown unfolded
                "rows": rows,
                "notes": list(self.notes),
                "question": copy.deepcopy(self.question),
                "ending": copy.deepcopy(self.ending),
            }


# ---- the questions, as the page draws them -----------------------------------------------------------------------


def _action(ident: str, label: str, tone: str) -> dict[str, str]:
    return {"id": ident, "label": label, "tone": tone}


def _box(label: str, value: str) -> dict[str, str]:
    return {"label": label, "value": value, "copy": "Copy", "copied": "Copied"}


def asked(prompt: str, *, secret: bool, hint: str, link: str) -> dict[str, Any]:
    title = prompt.strip().removesuffix(":").strip()
    return {
        "title": title,
        "text": hint,
        "boxes": [],
        "field": {"type": "password" if secret else "text"},
        "links": [{"label": "How to make it", "url": link}] if link else [],
        "actions": [_action("send", "Send", "primary")],
        "note": "",
    }


def code_question(secret: str, link: str) -> dict[str, Any]:
    return {
        "title": "Add the 6-digit code to your authenticator app",
        "text": "Shown once, now. The dashboard asks for a code from it to sign in a device and before each coded "
        "action, such as Push to prod.",
        "boxes": [_box("The code's secret", secret)],
        "field": None,
        "links": [{"label": "Open it in your authenticator app", "url": link}] if link.startswith("otpauth://") else [],
        "actions": [_action("done", "I added it", "primary")],
        "note": "",
    }


def _link(label: str, url: str) -> dict[str, str]:
    return {"label": label, "url": url}


def key_question(key: DeployKey, signed_in: bool, note: str) -> dict[str, Any]:
    access = "with write access" if key.write else "read-only"
    boxes = [_box(f"The public half: a deploy key on {key.repo}, {access}", key.public)]
    links = [] if signed_in else [_link(f"{key.repo}'s deploy keys", f"{_GITHUB}/{key.repo}/settings/keys/new")]
    add = _action("add", "Add it to GitHub", "")
    if key.private:
        title = f"The factory's read key, for {key.ci_repo}'s CI"
        text = (
            f"{key.why} Skip it when {key.ci_repo} already holds {key.secret} from an earlier install: it still works."
        )
        shown = f"The private half: {key.ci_repo}'s Actions secret {key.secret}. Shown once, now"
        boxes.append(_box(shown, key.private))
        if not signed_in:
            links.append(_link(f"{key.ci_repo}'s secrets", f"{_GITHUB}/{key.ci_repo}/settings/secrets/actions/new"))
        actions = [_action("skip", "Skip", "primary"), add if signed_in else _action("done", "I added both", "")]
    else:
        title, text = f"The warden's deploy key on {key.repo}", key.why
        if signed_in:
            actions = [{**add, "tone": "primary"}, _action("done", "I added it", "")]
        else:
            actions = [_action("done", "I added it", "primary")]
    text += " Add it to GitHub uses your signed-in gh." if signed_in else " Your gh is not signed in: add it on GitHub."
    return {"title": title, "text": text, "boxes": boxes, "field": None, "links": links, "actions": actions,
            "note": note}  # fmt: skip


# ---- the hosts a run uses ----------------------------------------------------------------------------------------


class Paged(Host):
    """A host whose lines, steps and questions go to the page instead of the terminal. It comes first in a console
    host's bases, so the host after it does every other thing."""

    page: Run

    def say(self, text: str) -> None:
        self.page.line(text)

    def step(self, number: int, title: str) -> None:
        self.page.step(title)

    def fail(self, program: str, problem: str) -> None:
        self.page.fail()

    def ask(self, prompt: str, *, secret: bool, hint: str, link: str) -> str:
        _, value = self.page.ask(asked(prompt, secret=secret, hint=hint, link=link))
        self.page.line(f"  you answered: {prompt.strip()}")
        return value.strip()

    def show_code(self, secret: str, link: str) -> None:
        self.page.ask(code_question(secret, link))
        self.page.line("  the 6-digit code's secret was shown")

    def show_key(self, key: DeployKey) -> None:
        owner = self.page.owner.name
        signed_in = self.query(["gh", "auth", "status", "--hostname", "github.com"], user=owner)[0] == 0
        note = ""
        while True:
            action, _ = self.page.ask(key_question(key, signed_in, note))
            if action != "add":
                self.page.line(f"  {'skipped' if action == 'skip' else 'you added'} the key {key.title} on {key.repo}")
                return
            try:
                self._add_key(key, owner)
            # telemetry: surfaced — the question shows what GitHub said, and the owner tries again or goes on
            except CommandError as exc:
                note = f"GitHub did not take it: {exc.output.strip()[-_SHOWN:]}"
                continue
            if not self.dry:
                self.page.line(f"  added the key {key.title} to {key.repo} on GitHub")
            return

    def _add_key(self, key: DeployKey, owner: str) -> None:
        """As the owner, with their gh: the deploy key, then the private half as the CI repo's secret."""
        folder = self.scratch(owner)
        public = folder / "key.pub"
        self.write(public, key.public + "\n", mode=0o644, owner=owner, group=self.page.owner.groups[0])
        try:
            self.run(["gh", "repo", "deploy-key", "add", str(public), "--repo", key.repo, "--title", key.title,
                      *(["--allow-write"] if key.write else [])], user=owner)  # fmt: skip
        finally:
            self.remove(folder)
        if key.private:
            self.run(["gh", "secret", "set", key.secret, "--repo", key.ci_repo], user=owner, stdin=key.private + "\n")

    def show_folder(self, label: str, folder: Path) -> None:
        self.page.folder = folder
        self.page.line(f"  {label}: {folder}")


class ConsoleHost(Paged, RealHost):
    """Changes the machine, as root, like the command line's host."""

    def __init__(self, page: Run) -> None:
        self.page = page


class ConsoleLookingHost(Paged, LookingHost):
    """The console's dry run: reads this machine for real and changes nothing."""

    def __init__(self, page: Run) -> None:
        LookingHost.__init__(self)
        self.page = page


class ConsoleDryHost(Paged, DryHost):
    """Changes nothing and reads only the facts given: what a test drives."""

    def __init__(self, page: Run, answers: dict[str, tuple[int, str]]) -> None:
        DryHost.__init__(self, answers=answers)
        self.page = page
