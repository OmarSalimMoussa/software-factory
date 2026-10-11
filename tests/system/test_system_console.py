from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from system_fakes import PASSING, SHA, ci_answers, ci_link, dry_job

from runtime.shared.paths import Places
from runtime.system import console_page, install, layout, uninstall
from runtime.system.console_app import Choices, Console, Hosts, Reply
from runtime.system.console_run import ConsoleDryHost, Run
from runtime.system.host import CommandError, DryHost, Host
from runtime.system.job import REPO_ROOT
from runtime.system.journal import Journal

PORT = 4321
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{HOST}"
SECRET = "sk-ant-oat01-typed-into-the-page-7f7f"


def _console(tmp_path: Path, real: Callable[[Run], Host] | None = None,
             runs: list[tuple[str, str, str]] = PASSING, opened: list[str] | None = None) -> Console:  # fmt: skip
    """The console offers SHA, whose CI ran ``runs``; what it opens for the owner goes to ``opened``."""
    job = dry_job(tmp_path)

    def dry(run: Run) -> Host:
        return ConsoleDryHost(run, dict(job.host.answers) if isinstance(job.host, DryHost) else {})

    reader = DryHost(answers={
        **ci_answers(REPO_ROOT, SHA, runs),
        f"git -C {REPO_ROOT} log -1 --format=%H%n%h%n%s {SHA} --": (0, f"{SHA}\n{SHA[:7]}\nThe offered commit\n"),
    })  # fmt: skip

    def opener(target: str) -> bool:
        if opened is not None:
            opened.append(target)
        return True

    console = Console(job.places, job.owner, reader, Hosts(real or dry, dry), opener, commit=SHA, repo=job.product,
                      source=None, logs=tmp_path / "logs", log_group="", choices=Choices())  # fmt: skip
    console.port = PORT
    return console


def _admit(console: Console) -> str:
    """The cookie the link leaves in the browser that opens it."""
    token = console.link().split("token=")[1]
    reply = console.handle("GET", f"/open?token={token}", {"host": HOST}, b"")
    assert reply.status == 303
    return reply.headers["Set-Cookie"].split(";")[0]


def _post(console: Console, cookie: str, path: str, body: dict[str, Any], **headers: str) -> Reply:
    sent = {"host": HOST, "cookie": cookie, "origin": ORIGIN, "content-type": "application/json", **headers}
    return console.handle("POST", path, sent, json.dumps(body).encode())


def _run(console: Console, cookie: str) -> dict[str, Any]:
    reply = console.handle("GET", "/api/run", {"host": HOST, "cookie": cookie}, b"")
    run: dict[str, Any] = json.loads(reply.body)["run"]
    return run


def _until(check: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 10
    while not check():
        assert time.monotonic() < deadline, "the console never got there"
        time.sleep(0.01)


def test_the_link_works_once(tmp_path: Path) -> None:
    console = _console(tmp_path)
    link = console.link()
    _admit(console)

    again = console.handle("GET", link.removeprefix(ORIGIN), {"host": HOST}, b"")

    assert again.status == 403 and "Set-Cookie" not in again.headers


@pytest.mark.parametrize(
    ("method", "path", "headers"),
    [
        ("GET", "/api/page", {"host": HOST}),  # no cookie
        ("GET", "/", {"host": HOST, "cookie": "factory-console=guessed"}),
        ("GET", "/api/page", {"host": "localhost:4321"}),  # a DNS-rebinding page names another host
        ("GET", "/open?token=guessed", {"host": HOST}),
        ("POST", "/api/dismiss", {"origin": "http://evil.example"}),
        ("POST", "/api/dismiss", {"origin": ""}),
        ("POST", "/api/dismiss", {"host": "127.0.0.1:9999"}),
    ],
    ids=["no-cookie", "wrong-cookie", "other-host", "wrong-token", "other-origin", "no-origin", "other-port"],
)
def test_the_console_answers_only_its_own_page(tmp_path: Path, method: str, path: str, headers: dict[str, str]) -> None:
    console = _console(tmp_path)
    cookie = _admit(console)
    sent = {"host": HOST, **({"cookie": cookie, "origin": ORIGIN, "content-type": "application/json"}
                              if method == "POST" else {}), **headers}  # fmt: skip
    sent = {name: value for name, value in sent.items() if value}

    refused = console.handle(method, path, sent, b"{}")

    assert refused.status == 403
    assert _post(console, cookie, "/api/dismiss", {}).status == 200  # the same request, from the page, is taken


@pytest.mark.parametrize(
    ("state", "mark"), [("waiting", "ring"), ("running", "spin"), ("done", "check"), ("failed", "x-mark")]
)
def test_each_phase_says_how_the_page_draws_it(tmp_path: Path, state: str, mark: str) -> None:
    console = _console(tmp_path)
    run = Run("install", "marks", True, ("A phase",), 1, console.owner, Journal(tmp_path / "m.log", terminal=False))
    run.rows[0].state = state
    assert run.view()["phases"][0]["mark"] == mark


def test_an_answer_reaches_the_step_that_asked_and_never_the_page_or_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    console = _console(tmp_path)
    cookie = _admit(console)
    log = tmp_path / "asking.log"
    run = Run("install", "asking", True, ("Connecting your accounts",), 1, console.owner, Journal(log, terminal=False))
    console.run = run
    host = ConsoleDryHost(run, {})
    got: list[str] = []
    asker = threading.Thread(target=lambda: got.append(host.ask("Your token: ", secret=True, hint="h", link="")))
    asker.start()
    _until(lambda: _run(console, cookie)["question"] is not None)
    question = _run(console, cookie)["question"]

    time.sleep(0.05)
    assert asker.is_alive() and not got  # the step waits for the page
    assert question["field"]["type"] == "password"
    answer = {"id": question["id"], "action": "send", "value": SECRET}
    assert _post(console, cookie, "/api/answer", answer).status == 200
    asker.join(5)

    assert got == [SECRET]
    shown = json.dumps(_run(console, cookie)) + json.dumps(console.page())
    assert SECRET not in shown + caplog.text + capsys.readouterr().out + log.read_text(encoding="utf-8")
    assert _post(console, cookie, "/api/answer", answer).status == 409  # answered once: only the step holds it


def _step(n: int, broken: bool) -> tuple[str, Callable[..., None]]:
    def act(ctx: Any) -> None:
        ctx.host.say(f"did {n}")
        if broken:
            ctx.host.say("$ false")
            raise CommandError(["false"], 1, "it broke")

    return f"Doing {n}", act


@pytest.mark.parametrize(
    ("first", "fails_at", "states", "ending"),
    [
        (True, None, ["done", "done", "done"], "The factory at"),
        (True, 4, ["done", "failed", "waiting", "done"], "taken back"),
        (False, 4, ["done", "failed", "waiting"], "Fix what it said"),  # a reinstall is never removed
    ],
    ids=["finished", "first-install-taken-back", "reinstall-kept"],
)
def test_an_install_in_the_console_shows_its_phases_never_its_commands_and_a_failures_plain_sentence_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: bool, fails_at: int | None, states: list[str],
    ending: str,
) -> None:  # fmt: skip
    steps = [_step(n, n == fails_at) for n in range(1, 6)]
    phases = (install.Phase("First", tuple(steps[:2])), install.Phase("Second", tuple(steps[2:4])),
              install.Phase("Third", tuple(steps[4:])))  # fmt: skip
    monkeypatch.setattr(install, "PHASES", phases)
    monkeypatch.setattr(install, "STEPS", tuple(steps))
    monkeypatch.setattr(install, "FIRST_CHANGE", 3)  # these steps change the machine from the third on
    monkeypatch.setattr(console_page, "address", lambda host: None)

    def real(run: Run) -> Host:
        host = ConsoleDryHost(run, {})
        host.dry = False  # decides like a real install; a dry host still changes nothing
        if not first:
            host.paths.add(layout.install_record(console.places.state))
        return host

    opened: list[str] = []
    console = _console(tmp_path, real, opened=opened)
    cookie = _admit(console)
    body = {"commit": SHA, "repo": str(console.repo), "healthchecks": False, "keep": False, "dry": False}

    assert _post(console, cookie, "/api/install", body).status == 200
    _until(lambda: _run(console, cookie)["ending"] is not None)

    run = _run(console, cookie)
    rolled = [install.ROLL_BACK] if len(states) > 3 else []
    assert [phase["title"] for phase in run["phases"]] == ["First", "Second", "Third", *rolled]
    assert [phase["state"] for phase in run["phases"]] == states
    assert ending in run["ending"]["headline"] + run["ending"]["next"]
    [log] = (tmp_path / "logs").iterdir()
    kept = log.read_text(encoding="utf-8")
    assert all(f"did {n}" in kept for n in range(1, (fails_at or 5) + 1))
    assert "did " not in json.dumps(run) and "$ false" not in json.dumps(run)
    if fails_at is None:
        assert run["progress"] == 100 and [action["id"] for action in run["ending"]["actions"]] == ["dismiss"]
        return
    said = "The install could not finish doing 4: a command it ran failed. Run it again; if it stops here again, the "
    assert run["ending"]["detail"] == said + "log says why."
    assert run["ending"]["log"] == f"Every detail is in the log: {log}" and run["log"] == "Show the log"
    assert [action["id"] for action in run["ending"]["actions"]] == ["log", "dismiss"]
    assert _post(console, cookie, "/api/open", {"what": "log"}).status == 200 and opened == [str(log)]


def test_uninstall_removes_only_after_the_owner_saw_what_it_removes(tmp_path: Path) -> None:
    console = _console(tmp_path)
    cookie = _admit(console)

    assert _post(console, cookie, "/api/uninstall", {"confirm": True, "repo": ""}).status == 409
    assert _post(console, cookie, "/api/uninstall", {"confirm": False, "repo": ""}).status == 200
    _until(lambda: _run(console, cookie)["ending"] is not None)
    look = _run(console, cookie)

    assert look["kind"] == "look" and [phase["title"] for phase in look["phases"]] == list(uninstall.STEPS)
    assert [a["id"] for a in look["ending"]["actions"]] == ["uninstall", "dismiss"]
    assert _post(console, cookie, "/api/uninstall", {"confirm": True, "repo": ""}).status == 200


def test_the_console_shows_the_commits_ci_and_installs_it_only_once_ci_passed(tmp_path: Path) -> None:
    console = _console(tmp_path, runs=[("test", "completed", "failure"), ("drives", "completed", "success")])
    cookie = _admit(console)
    body = {"commit": SHA, "repo": str(console.repo), "healthchecks": False, "keep": False, "dry": False}

    shown = console.page()["install"]["commit"]["ci"]
    refused = _post(console, cookie, "/api/install", body)

    assert (shown["state"], shown["links"]) == ("failed", [{"label": "test: failed (failure)", "url": ci_link(0)}])
    assert refused.status == 409 and f"  test: failed (failure): {ci_link(0)}" in json.loads(refused.body)["reason"]
    assert console.run is None
    assert _post(console, cookie, "/api/install", {**body, "dry": True}).status == 200  # a dry run changes nothing
    console.stop()


@pytest.mark.parametrize(
    ("published", "checked"),
    [
        # An older release's settings: without the keys this one added, with one it dropped.
        ({"factory": {"clone": "/x"}, "watch": {"http_timeout_seconds": 1},
          "dashboard": {"warden_seconds": 1, "port": 8400}}, True),
        ({"watch": {"http_timeout_seconds": 1}, "dashboard": {"warden_seconds": 1}}, False),  # no port to check
        ({"watch": {"http_timeout_seconds": 0}, "dashboard": {"warden_seconds": 1, "port": 8400}}, False),
    ],
    ids=["older-release", "port-missing", "timeout-out-of-range"],
)  # fmt: skip
def test_the_status_checks_an_older_install_by_the_few_settings_it_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, published: dict[str, Any], checked: bool
) -> None:
    places = Places.under(tmp_path)
    record = {"commit": SHA, "subject": "An older release", "drills": []}
    layout.install_record(places.state).parent.mkdir(parents=True)
    layout.install_record(places.state).write_text(json.dumps(record))
    (places.state / "public").mkdir()
    (places.state / "public" / "settings.json").write_text(json.dumps(published))
    monkeypatch.setattr(console_page, "_answers", lambda url, timeout, any_status: False)  # nothing runs here

    found = console_page.status(places, DryHost())

    if checked:
        assert found["problem"] == ""
        assert [s["name"] for s in found["services"]] == ["The warden", "The dashboard", "GlitchTip", "Langfuse"]
    else:
        assert found["services"] == [] and "were not checked" in found["problem"]
