from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from system_fakes import dry_job

from runtime.system import console_page, install, layout, uninstall
from runtime.system.console_app import Console, Hosts, Reply
from runtime.system.console_run import ConsoleDryHost, Run
from runtime.system.host import CommandError, DryHost, Host

PORT = 4321
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{HOST}"
SECRET = "sk-ant-oat01-typed-into-the-page-7f7f"


def _console(tmp_path: Path, real: Callable[[Run], Host] | None = None) -> Console:
    job = dry_job(tmp_path)

    def dry(run: Run) -> Host:
        return ConsoleDryHost(run, dict(job.host.answers) if isinstance(job.host, DryHost) else {})

    console = Console(job.places, job.owner, DryHost(), Hosts(real or dry, dry), lambda target: True, commit="main",
                      repo=job.product)  # fmt: skip
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


def test_an_answer_reaches_the_step_that_asked_and_never_the_page_or_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    console = _console(tmp_path)
    cookie = _admit(console)
    run = Run("install", "asking", True, ("Keep the secrets",), console.owner, "")
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
    assert SECRET not in shown + caplog.text + capsys.readouterr().out
    assert _post(console, cookie, "/api/answer", answer).status == 409  # answered once: only the step holds it


def _step(n: int, broken: bool) -> tuple[str, Callable[..., None]]:
    def act(ctx: Any) -> None:
        ctx.host.say(f"  did {n}")
        if broken:
            raise CommandError(["false"], 1, "it broke")

    return f"Step {n}", act


@pytest.mark.parametrize(
    ("first", "fails_at", "states", "ending"),
    [
        (True, None, ["done"] * 5, "The factory at"),
        (True, 4, ["done", "done", "done", "failed", "waiting", *["done"] * len(uninstall.STEPS)], "taken back"),
        (False, 4, ["done", "done", "done", "failed", "waiting"], "Fix what it said"),  # a reinstall is never removed
    ],
    ids=["finished", "first-install-taken-back", "reinstall-kept"],
)
def test_an_install_in_the_console_shows_each_step_in_order_and_a_failures_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: bool, fails_at: int | None, states: list[str],
    ending: str,
) -> None:  # fmt: skip
    monkeypatch.setattr(install, "STEPS", tuple(_step(n, n == fails_at) for n in range(1, 6)))
    monkeypatch.setattr(console_page, "address", lambda host: None)

    def real(run: Run) -> Host:
        host = ConsoleDryHost(run, {})
        host.dry = False  # decides like a real install; a dry host still changes nothing
        if not first:
            host.paths.add(layout.install_record(console.places.state))
        return host

    console = _console(tmp_path, real)
    cookie = _admit(console)
    body = {"commit": "main", "repo": str(console.repo), "healthchecks": False, "keep": False, "dry": False}

    assert _post(console, cookie, "/api/install", body).status == 200
    _until(lambda: _run(console, cookie)["ending"] is not None)

    run = _run(console, cookie)
    rolled = [row for row in run["rows"] if row["group"]]
    assert [row["title"] for row in run["rows"]] == [f"Step {n}" for n in range(1, 6)] + [r["title"] for r in rolled]
    assert [row["title"] for row in rolled] == (list(uninstall.STEPS) if len(states) > 5 else [])
    assert [row["state"] for row in run["rows"]] == states
    assert [line for row in run["rows"][:5] for line in row["lines"]][: fails_at or 5] == [
        f"  did {n}" for n in range(1, (fails_at or 5) + 1)
    ]
    assert ending in run["ending"]["headline"] + run["ending"]["next"]


def test_uninstall_removes_only_after_the_owner_saw_what_it_removes(tmp_path: Path) -> None:
    console = _console(tmp_path)
    cookie = _admit(console)

    assert _post(console, cookie, "/api/uninstall", {"confirm": True, "repo": ""}).status == 409
    assert _post(console, cookie, "/api/uninstall", {"confirm": False, "repo": ""}).status == 200
    _until(lambda: _run(console, cookie)["ending"] is not None)
    look = _run(console, cookie)

    assert look["kind"] == "look" and [row["title"] for row in look["rows"]] == list(uninstall.STEPS)
    assert [a["id"] for a in look["ending"]["actions"]] == ["uninstall", "dismiss"]
    assert _post(console, cookie, "/api/uninstall", {"confirm": True, "repo": ""}).status == 200
