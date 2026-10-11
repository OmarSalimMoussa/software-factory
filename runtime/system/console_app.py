"""The install console's answers: who may use it, what its page shows, and the one action it runs at a time.

Only the console's own address is answered. Past the link it prints, which works once, only the browser that opened
the link is answered, by the cookie the link left there; and a change only from the console's own page.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from runtime.shared.paths import Places
from runtime.system import ci_check, console_page, install, release, sealed, uninstall
from runtime.system.console_run import Run
from runtime.system.host import Account, Host
from runtime.system.job import REPO_ROOT
from runtime.system.journal import Journal

log = logging.getLogger("factory.console")

COOKIE = "factory-console"
UI = Path(__file__).resolve().parent / "console_ui"
DASHBOARD_UI = REPO_ROOT / "runtime" / "dashboard" / "ui"


@dataclass(frozen=True)
class Choices:
    """What the console was started with: the install form's starting choices and the settings each install saves."""

    healthchecks: bool = False
    keep: bool = False
    dry: bool = False
    changes: dict[str, Any] = field(default_factory=dict)


FILES = {
    "/": (UI / "console.html", "text/html; charset=utf-8"),
    "/console.css": (UI / "console.css", "text/css; charset=utf-8"),
    "/console.js": (UI / "console.js", "text/javascript"),
    "/app.css": (DASHBOARD_UI / "app.css", "text/css; charset=utf-8"),
    "/icons.js": (DASHBOARD_UI / "icons.js", "text/javascript"),
}
BUSY = "An action is running. Wait for it to end."


@dataclass
class Reply:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def _json(status: int, data: dict[str, Any], **headers: str) -> Reply:
    return Reply(status, json.dumps(data).encode(), {"Content-Type": "application/json", **headers})


def refused(status: int, reason: str, *, api: bool = True) -> Reply:
    if api:
        return _json(status, {"ok": False, "reason": reason})
    return Reply(status, reason.encode(), {"Content-Type": "text/plain; charset=utf-8"})


@dataclass(frozen=True)
class Hosts:
    """The host an action runs with: one that changes the machine, and one that only reads it."""

    real: Callable[[Run], Host]
    dry: Callable[[Run], Host]


class Console:
    """``source`` is the sealed copy it runs from (None in a test); ``logs`` is where each action's log goes, readable
    by ``log_group`` ("" leaves the folder's group alone)."""

    def __init__(self, places: Places, owner: Account, reader: Host, hosts: Hosts, opener: Callable[[str], bool], *,
                 commit: str | None, repo: Path | None, source: sealed.Staged | None, logs: Path, log_group: str,
                 choices: Choices) -> None:  # fmt: skip
        self.places, self.owner, self.reader, self.hosts, self.opener = places, owner, reader, hosts, opener
        self.commit, self.repo, self.source, self.choices = commit, repo, source, choices
        self.logs, self.log_group = logs, log_group
        self.release = release.read_manifest(source.release) if source and source.release else None
        # The owner's factory checkout, for a console that runs from one: its commits and its CI.
        self.checkout = source.checkout if source and source.checkout else REPO_ROOT
        self.port = 0  # set once the server is bound
        self.stopped = threading.Event()
        self.run: Run | None = None
        self._token: str | None = secrets.token_urlsafe(32)
        self._session = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._looked: Path | None = None  # the product checkout the last look at an uninstall was for
        self.kept = False  # a failed install the owner keeps: the console's sealed copy stays beside it
        self._files = {path: (file.read_bytes(), media) for path, (file, media) in FILES.items()}

    def link(self) -> str:
        if self._token is None:
            raise RuntimeError("the console's link was used already")
        return f"http://127.0.0.1:{self.port}/open?token={self._token}"

    # ---- every request -------------------------------------------------------------------------------------------

    def handle(self, method: str, target: str, headers: Mapping[str, str], body: bytes) -> Reply:
        """Header names in lower case. Only the console's own address is answered; past the link, only the browser
        that opened it, and a change only from the console's own page."""
        split = urlsplit(target)
        api = split.path.startswith("/api/")
        if headers.get("host") != f"127.0.0.1:{self.port}":
            return refused(403, "This address is not the console's.", api=api)
        admitted = self._admitted(headers.get("cookie", ""))
        if method == "GET" and split.path == "/open":
            if admitted:  # the browser the link admitted may open it again; to any other, a used link is void
                return Reply(303, b"", {"Location": "/"})
            return self._open(dict(parse_qsl(split.query)).get("token", ""))
        if not admitted:
            return refused(403, "Open the link the console printed in its terminal.", api=api)
        if method == "GET":
            return self._get(split.path)
        if method != "POST" or not api:
            return refused(405, "The console takes no such request.", api=api)
        if headers.get("origin") != f"http://127.0.0.1:{self.port}":
            return refused(403, "A change must come from the console's own page.")
        if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return refused(415, "A change is sent as JSON.")
        try:
            data = json.loads(body)
        # telemetry: surfaced — the page is told its change was not JSON
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return refused(400, "A change is one JSON object.")
        return self._post(split.path, data)

    def _open(self, token: str) -> Reply:
        with self._lock:
            good = self._token is not None and hmac.compare_digest(token.encode(), self._token.encode())
            if good:
                self._token = None
        if not good:
            reason = "This link was used already, or is not the console's. Start the console again for a new link."
            return refused(403, reason, api=False)
        cookie = f"{COOKIE}={self._session}; Path=/; HttpOnly; SameSite=Strict"
        return Reply(303, b"", {"Location": "/", "Set-Cookie": cookie})

    def _admitted(self, header: str) -> bool:
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(header)
        # telemetry: expected — a header the browser mangled admits no one
        except CookieError:
            return False
        held = jar.get(COOKIE)
        return held is not None and hmac.compare_digest(held.value.encode(), self._session.encode())

    def _get(self, path: str) -> Reply:
        if path == "/api/page":
            return _json(200, self.page())
        if path == "/api/run":
            with self._lock:
                run = self.run
            return _json(200, {"ok": True, "run": run.view() if run else None, "busy": self._busy()})
        if path in self._files:
            body, media = self._files[path]
            return Reply(200, body, {"Content-Type": media})
        return refused(404, "There is no such page.", api=path.startswith("/api/"))

    def _post(self, path: str, data: dict[str, Any]) -> Reply:
        routes: dict[str, Callable[[dict[str, Any]], Reply]] = {
            "/api/install": self.start_install,
            "/api/uninstall": self.start_uninstall,
            "/api/answer": self.answer,
            "/api/open": self.open_folder,
            "/api/dismiss": self.dismiss,
            "/api/close": self.close,
        }
        route = routes.get(path)
        return route(data) if route else refused(404, "There is no such action.")

    # ---- what the page shows -------------------------------------------------------------------------------------

    def page(self) -> dict[str, Any]:
        try:
            record = console_page.read_record(self.places)
        # telemetry: surfaced — the status draws the record that cannot be read
        except (OSError, ValueError):
            record = None
        with self._lock:
            run = self.run
        return {
            "ok": True,
            "title": "Install console",
            "sub": "Install, reinstall or remove the factory on this laptop. Nothing reaches the server.",
            "close": "Close",
            "status_label": "This laptop",
            "status": console_page.status(self.places, self.reader),
            "install": console_page.install_form(
                *self._offer(),
                self.repo,
                record,
                {"healthchecks": self.choices.healthchecks, "keep": self.choices.keep, "dry": self.choices.dry},
            ),
            "uninstall": {
                "heading": "Uninstall",
                "text": "Removes everything the installer made, so the next install starts from a clean machine. It "
                "first shows what it would remove, changing nothing, and removes it only when you confirm.",
                "button": "Uninstall…",
            },
            "run": run.view() if run else None,
            "busy": self._busy(),
        }

    def _offer(self) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """The commit this console installs and its CI: a release's own, or a checkout's, asked of GitHub."""
        if self.release is not None:
            return console_page.release_offer(self.release), console_page.RELEASE_CI
        offered = console_page.checkout_offer(self.reader, self.owner, self.checkout, self.commit)
        return offered, console_page.checkout_ci(self.reader, self.owner, self.checkout, offered)

    def _journal(self, kind: str) -> Journal:
        return Journal.create(self.logs, kind, group=self.log_group, verbose=False, terminal=False)

    def _busy(self) -> bool:
        run = self.run
        return run is not None and not run.finished

    def _refusal(self) -> str | None:
        """Why no action may start now; None when one may."""
        return BUSY if self._busy() else None

    # ---- actions -------------------------------------------------------------------------------------------------

    def start_install(self, data: dict[str, Any]) -> Reply:
        commit, repo = data.get("commit"), data.get("repo")
        flags = {name: data.get(name) for name in ("healthchecks", "keep", "dry")}
        problems = []
        if not isinstance(commit, str) or not commit.strip():
            problems.append("Name the factory commit to install.")
        if not isinstance(repo, str) or not Path(repo.strip()).is_absolute():
            problems.append("Name your product checkout by its full path.")
        if not all(isinstance(flag, bool) for flag in flags.values()):
            problems.append("Each choice is on or off.")
        staged = self.source.sha if self.source else ""
        if isinstance(commit, str) and staged and not staged.startswith(commit.strip()) and commit.strip() != "":
            problems.append(f"This console runs {staged[:12]} and installs only that commit: to install another, "
                            "start the console with --commit <that commit>.")  # fmt: skip
        if problems or not isinstance(commit, str) or not isinstance(repo, str):
            return refused(400, " ".join(problems))
        dry, keep = flags["dry"] is True, flags["keep"] is True
        if not dry and self.release is None:
            origin = ci_check.origin(self.reader, self.owner.name, self.checkout)
            found = ci_check.verdict(self.reader, self.owner, origin, commit.strip())
            if found.state != ci_check.PASSED:
                return refused(409, console_page.sentence(found.refusal()))
        with self._lock:
            if why := self._refusal():
                return refused(409, why)
            phases = tuple(phase.title for phase in install.PHASES)
            run = Run("install", "", dry, phases, len(install.STEPS), self.owner, self._journal("install"))
            host = (self.hosts.dry if dry else self.hosts.real)(run)
            ctx = install.job(host, self.places, self.owner, Path(repo.strip()), commit.strip(),
                              dict(self.choices.changes), flags["healthchecks"] is True)  # fmt: skip
            verb = "Installing" if ctx.first else "Reinstalling"
            run.title = f"{'Dry run: ' if dry else ''}{verb} the factory at {commit.strip()[:12]}"

            def act() -> dict[str, Any]:
                problem = install.perform(ctx, keep=keep)
                if not dry:
                    self.kept = problem is not None and keep
                return console_page.install_ending(ctx, problem, keep, run, self.reader)

            self._start(run, act)
        return _json(200, {"ok": True})

    def start_uninstall(self, data: dict[str, Any]) -> Reply:
        """First a look, changing nothing; then, confirmed, the uninstall of what that look showed."""
        confirm, repo = data.get("confirm"), data.get("repo")
        if not isinstance(confirm, bool) or not isinstance(repo, str) or (repo and not Path(repo).is_absolute()):
            return refused(400, "Say whether you confirm, and name your product checkout by its full path, or none.")
        with self._lock:
            if why := self._refusal():
                return refused(409, why)
            last = self.run
            if confirm and not (last and last.kind == "look" and last.ending and last.ending["ok"]):
                return refused(409, "Look at what Uninstall removes first.")
            checkout = self._looked if confirm else (Path(repo) if repo else None)
            self._looked = checkout
            kind, title = ("uninstall", "Uninstalling") if confirm else ("look", "What Uninstall removes")
            run = Run(kind, title, not confirm, uninstall.STEPS, len(uninstall.STEPS), self.owner,
                      self._journal("uninstall"))  # fmt: skip
            host = (self.hosts.real if confirm else self.hosts.dry)(run)

            def act() -> dict[str, Any]:
                ending = console_page.uninstall_ending(run, uninstall.perform(host, self.places, self.owner, checkout))
                if confirm:
                    self.kept = False  # no failed install is left for the sealed copy to stay beside
                return ending

            self._start(run, act)
        return _json(200, {"ok": True})

    def _start(self, run: Run, act: Callable[[], dict[str, Any]]) -> None:
        self.run = run

        def work() -> None:
            try:
                ending, broke = act(), False
            except Exception as exc:
                log.exception("console: the %s broke (%s)", run.kind, type(exc).__name__)
                run.journal.line(f"the console broke: {type(exc).__name__}: {exc}")
                ending, broke = console_page.broken(run, exc), True
            run.finish(ending, broke=broke)

        self._worker = threading.Thread(target=work, name=f"console-{run.kind}", daemon=True)
        self._worker.start()

    def answer(self, data: dict[str, Any]) -> Reply:
        question, action, value = data.get("id"), data.get("action"), data.get("value")
        if not all(isinstance(each, str) for each in (question, action, value)):
            return refused(400, "An answer names its question and action, with the field's text.")
        with self._lock:
            run = self.run
        if run is None:
            return refused(409, "Nothing is asking.")
        why = run.respond(str(question), str(action), str(value))
        return _json(200, {"ok": True}) if why is None else refused(409, why)

    def open_folder(self, data: dict[str, Any]) -> Reply:
        """The run's log, or the folder a step named (the drill sessions' output), in the owner's desktop."""
        with self._lock:
            run = self.run
        what = data.get("what")
        target = (run.journal.path if what == "log" else run.folder if what == "logs" else None) if run else None
        if target is None:
            return refused(409, "There is nothing of that kind to open.")
        if not self.opener(str(target)):
            return refused(409, f"Your desktop session could not be found: open {target} yourself.")
        return _json(200, {"ok": True})

    def dismiss(self, data: dict[str, Any]) -> Reply:
        with self._lock:
            if self._busy():
                return refused(409, BUSY)
            self.run = None
        return _json(200, {"ok": True})

    def close(self, data: dict[str, Any]) -> Reply:
        with self._lock:
            if self._busy():
                return refused(409, "An action is running. Close the console once it ends.")
        self.stopped.set()
        return _json(200, {"ok": True, "message": "The console stopped. You can close this tab."})

    def stop(self) -> None:
        """Every question waiting is refused, so a running action ends the way a failed one does; then it waits."""
        with self._lock:
            run, worker = self.run, self._worker
        if run is not None:
            run.close()
        if worker is not None and worker.is_alive():
            print(f"The {run.kind if run else 'action'} is still running: the console stops once it ends. Ctrl-C again "
                  "stops it now.", flush=True)  # fmt: skip
            worker.join()
