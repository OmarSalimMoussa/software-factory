"""The tester's browser wrapper: the trace starts before the session's first call, and the tester can neither see nor
stop it; the factory's tools sit beside Playwright's, never show a sign-in link, and always answer; the wrapper stops
when the trace does not start or the browser stops."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from queue_kit import World

from runtime.qa import qa_browser, wrapper
from runtime.qa.wrapper import ServerError
from runtime.qa.wrapper_tools import NOTES, Tools
from runtime.shared.settings import Settings

FAKE = Path(__file__).with_name("fake_mcp.py")
LINK = "https://app.test/auth/link/one-time-secret"


class Session:
    """The tester's session, talking to the wrapper over two pipes, the wrapper running the fake server and equipped
    as the tester's browser is."""

    def __init__(self, log: Path, settings: Settings, link_for: Callable[[str], str], kept: list[str]) -> None:
        read_in, write_in = os.pipe()
        read_out, write_out = os.pipe()
        self.to_wrapper, self.from_wrapper = os.fdopen(write_in, "w"), os.fdopen(read_out)
        client_in, client_out = os.fdopen(read_in), os.fdopen(write_out, "w")
        equip = qa_browser.equipper(link_for, kept.append, settings)
        argv = [sys.executable, str(FAKE), str(log)]
        self.exit: list[int] = []
        self.thread = threading.Thread(
            target=lambda: self.exit.append(wrapper.serve(argv, equip, 10, 10_000, client_in, client_out))
        )
        self.thread.start()
        self.sent = 0

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.post(method, params)
        answer: dict[str, Any] = json.loads(self.from_wrapper.readline())
        return answer

    def post(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.sent += 1
        self._write({"jsonrpc": "2.0", "id": self.sent, "method": method, "params": params or {}})

    def notify(self, method: str) -> None:
        self._write({"jsonrpc": "2.0", "method": method})

    def answer(self, key: Any, result: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": key, "result": result})

    def _write(self, message: dict[str, Any]) -> None:
        self.to_wrapper.write(json.dumps(message) + "\n")
        self.to_wrapper.flush()

    def close(self) -> int:
        self.to_wrapper.close()
        self.thread.join(10)
        return self.exit[0]


def _open(tmp_path: Path, world: World, link_for: Callable[[str], str], kept: list[str]) -> Session:
    session = Session(tmp_path / "server.log", world.ctx().settings(), link_for, kept)
    session.send("initialize", {"protocolVersion": "2025-06-18"})
    session.notify("notifications/initialized")
    return session


@pytest.fixture
def opened(tmp_path: Path, world: World) -> Iterator[tuple[Session, Path, list[str]]]:
    kept: list[str] = []
    session = _open(tmp_path, world, lambda address: LINK, kept)
    yield session, tmp_path / "server.log", kept
    session.from_wrapper.close()


def test_the_trace_starts_before_the_first_call_and_the_tester_can_neither_see_nor_stop_it(
    opened: tuple[Session, Path, list[str]],
) -> None:
    session, log, _ = opened
    tools = session.send("tools/list")["result"]["tools"]
    listed = [t["name"] for t in tools]
    stopped = session.send("tools/call", {"name": "browser_stop_tracing", "arguments": {}})
    closed = session.send("tools/call", {"name": "browser_close", "arguments": {}})
    session.send("tools/call", {"name": "browser_navigate", "arguments": {"url": "https://app.test/"}})
    assert session.close() == 0
    assert listed == [
        "browser_navigate",
        "browser_snapshot",
        "browser_resize",
        "browser_type",
        "browser_mouse_click_xy",
        "browser_wait_for",
        "browser_sign_in",
        "browser_wait_until",
        "browser_open_desktop",
        "browser_color_scheme",
        "browser_mouse_press_xy",
        "browser_type_keys",
        "browser_hold_request",
        "browser_release_request",
    ]
    # A tester that loads Playwright's typing or clicking tool reads which of the factory's works on a remote desktop.
    noted = {t["name"]: t["description"] for t in tools if t["name"] in NOTES}
    assert noted == {name: "Playwright's own. " + note for name, note in NOTES.items()}
    assert "browser_type_keys" in noted["browser_type"] and "browser_mouse_press_xy" in noted["browser_mouse_click_xy"]
    # The guard refuses a named file, so no tool offers one.
    playwrights = [t["inputSchema"]["properties"] for t in tools if t["description"].startswith("Playwright's own.")]
    assert playwrights and all(fields == {"type": {"type": "string"}} for fields in playwrights)
    assert stopped["error"]["message"] == "there is no tool browser_stop_tracing"
    assert closed["error"]["message"] == "there is no tool browser_close"  # closing would end the trace
    assert log.read_text().splitlines() == [
        "initialize",
        "notifications/initialized",
        "tools/call browser_start_tracing",
        "tools/list",
        "tools/call browser_navigate",
    ]


def test_signing_in_opens_a_fresh_link_the_tester_never_sees_and_keeps_it_for_the_replay(
    opened: tuple[Session, Path, list[str]],
) -> None:
    session, log, kept = opened
    answer = session.send("tools/call", {"name": "browser_sign_in", "arguments": {"address": "tester-1@example.com"}})
    # A page left on the link names it in every answer after; the session still never sees it.
    later = session.send("tools/call", {"name": "browser_snapshot", "arguments": {}})
    assert session.close() == 0
    said = answer["result"]["content"][0]["text"]
    assert said.startswith("Signed in as tester-1@example.com.") and "Page URL: the sign-in link" in said
    assert later["result"]["content"][0]["text"] == "### Page\n- Page URL: the sign-in link\n"
    assert LINK not in json.dumps([answer, later]) and kept == [LINK]
    assert log.read_text().splitlines()[-2:] == ["tools/call browser_navigate", "tools/call browser_snapshot"]


def test_a_factory_tool_that_breaks_still_answers(tmp_path: Path, world: World) -> None:
    def warden_away(address: str) -> str:
        raise ConnectionRefusedError("the warden's socket refused")

    session = _open(tmp_path, world, warden_away, [])
    answer = session.send("tools/call", {"name": "browser_sign_in", "arguments": {"address": "tester-1@example.com"}})
    assert session.close() == 0
    said = answer["result"]
    assert said["isError"] and said["content"][0]["text"] == (
        "the factory's browser_sign_in broke (ConnectionRefusedError); report the step blocked with that"
    )


def test_a_browser_that_asks_the_session_something_as_its_trace_starts_still_lists_its_tools(
    tmp_path: Path, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASK_ROOTS", "1")
    session = _open(tmp_path, world, lambda address: LINK, [])
    asked = json.loads(session.from_wrapper.readline())
    session.answer(asked["id"], {"roots": []})

    listed: list[dict[str, Any]] = []
    asking = threading.Thread(target=lambda: listed.append(session.send("tools/list")), daemon=True)
    asking.start()
    asking.join(5)

    assert asked["method"] == "roots/list"
    assert listed, "no tool list: the wrapper waited for the trace on the thread that reads the session"
    assert "browser_navigate" in [t["name"] for t in listed[0]["result"]["tools"]]
    assert session.close() == 0


def test_a_trace_that_does_not_start_stops_the_browser(
    tmp_path: Path, world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FAIL_TRACE", "1")
    session = _open(tmp_path, world, lambda address: LINK, [])
    assert session.close() == 1
    assert "the trace did not start, so this browser stops: ### Error" in capsys.readouterr().err


def test_a_call_waits_for_the_trace_and_never_reaches_a_browser_whose_trace_did_not_start(
    tmp_path: Path, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAIL_TRACE", "1")
    session = _open(tmp_path, world, lambda address: LINK, [])
    session.post("tools/call", {"name": "browser_navigate", "arguments": {"url": "https://app.test/"}})

    assert session.close() == 1
    assert "tools/call browser_navigate" not in (tmp_path / "server.log").read_text()


def test_the_wrapper_stops_when_the_browser_does(
    opened: tuple[Session, Path, list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    session, _, _ = opened
    session.post("tools/call", {"name": "browser_crash", "arguments": {}})  # the fake server exits on it
    session.thread.join(10)
    assert session.exit == [1]
    assert "factory qa-browser: the browser stopped (exit 3)" in capsys.readouterr().err


def _proxy(wait: float = 0.01) -> tuple[wrapper.Proxy, io.StringIO, io.StringIO]:
    to_server, to_session = io.StringIO(), io.StringIO()
    return wrapper.Proxy(to_server, to_session, wait, 40), to_server, to_session


@pytest.mark.parametrize(("text", "cut"), [("a" * 40, False), ("b" * 41, True)], ids=["at-the-cap", "past-it"])
def test_a_browser_answer_past_the_factorys_cap_reaches_the_tester_cut_with_a_note(text: str, cut: bool) -> None:
    proxy, _, to_session = _proxy(wait=10)
    proxy.from_client(_call(7, "browser_snapshot"))
    shot = {"type": "image", "data": "c" * 100, "mimeType": "image/png"}
    proxy.from_server(
        json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": text}, shot]}})
    )
    [answer] = _said(to_session)
    assert answer["result"]["content"] == [{"type": "text", "text": text[:40] + (wrapper.CUT if cut else "")}, shot]


def test_an_answer_the_wrapper_stopped_waiting_for_reaches_no_one() -> None:
    proxy, _, to_session = _proxy()
    with pytest.raises(ServerError, match="did not answer tools/call"):
        proxy.request("tools/call", {"name": "browser_snapshot", "arguments": {}})
    proxy.from_server(json.dumps({"jsonrpc": "2.0", "id": "factory-1", "result": {"content": []}}))
    assert to_session.getvalue() == ""


def test_a_cancelled_factory_call_stops_and_gets_no_answer() -> None:
    proxy, to_server, to_session = _proxy()
    stopped = threading.Event()

    def answer(name: str, args: dict[str, Any], cancelled: threading.Event) -> tuple[str, bool]:
        stopped.set() if cancelled.wait(10) else None
        return "Waited", False

    proxy.add_tools([{"name": "browser_wait_until"}, {"name": "browser_open_desktop"}], answer, {})
    proxy.from_client(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "browser_wait_until", "arguments": {}},
            }
        )
    )
    proxy.from_client(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}}))
    assert stopped.wait(10)
    assert to_server.getvalue() == ""  # the server never saw the call, so it is not told it was cancelled


def _call(key: int, name: str, **args: Any) -> str:
    params = {"name": name, "arguments": args}
    return json.dumps({"jsonrpc": "2.0", "id": key, "method": "tools/call", "params": params})


def _said(to: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in to.getvalue().splitlines()]


def test_a_call_the_browser_does_not_answer_in_time_fails_and_its_late_answer_reaches_no_one() -> None:
    proxy, to_server, to_session = _proxy()
    proxy.from_client(_call(7, "browser_evaluate", function="() => new Promise(() => {})"))
    for _ in range(500):
        if to_session.getvalue():
            break
        threading.Event().wait(0.01)
    [answer] = _said(to_session)
    assert answer["id"] == 7 and answer["result"]["isError"]
    assert "did not answer browser_evaluate within 0 s" in answer["result"]["content"][0]["text"]
    # The browser is told to stop the call, and its answer, should it still come, goes nowhere.
    assert _said(to_server)[-1] == {"jsonrpc": "2.0", "method": "notifications/cancelled",
                                    "params": {"requestId": 7, "reason": "no answer within 0 s"}}  # fmt: skip
    proxy.from_server(json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"content": []}}))
    assert len(_said(to_session)) == 1


def test_a_wait_has_its_own_time_to_answer_beside_the_browsers() -> None:
    proxy, _, to_session = _proxy()
    proxy.from_client(_call(8, "browser_wait_for", time=30))
    threading.Event().wait(0.2)
    assert to_session.getvalue() == ""
    proxy.from_server(json.dumps({"jsonrpc": "2.0", "id": 8, "result": {"content": []}}))
    assert _said(to_session) == [{"jsonrpc": "2.0", "id": 8, "result": {"content": []}}]
    proxy.close()


class Ticks(threading.Event):
    """A cancel flag whose waits pass on a clock of their own, and never end early."""

    def __init__(self) -> None:
        super().__init__()
        self.clock = 0.0

    def wait(self, timeout: float | None = None) -> bool:
        self.clock += timeout or 0
        return self.is_set()


class Browser:
    """Playwright's tools as the factory's tools call them: each call kept, each snapshot the next page given."""

    def __init__(self, pages: list[str], results: tuple[Any, ...] = ()) -> None:
        self.pages, self.results, self.ticks = iter(pages), iter(results), Ticks()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, name: str, args: dict[str, Any]) -> str:
        self.calls.append((name, args))
        if name == "browser_snapshot":
            return next(self.pages)
        # The code tool's value as Playwright prints it.
        return next((f"### Result\n{json.dumps(r)}\n### Ran Playwright code" for r in self.results), f"{name} done")

    def answer(
        self, name: str, args: dict[str, Any], reload_seconds: int = 60, max_minutes: int = 15
    ) -> tuple[str, bool]:
        tools = Tools(
            self.call,
            lambda address: LINK,
            lambda link: None,
            reload_seconds,
            max_minutes,
            (1280, 800),
            40,
            30,
            150,
            250,
            lambda: self.ticks.clock,
        )
        return tools.answer(name, args, self.ticks)


def test_a_wait_reloads_until_its_words_show_in_one_call() -> None:
    page = "- Page URL: https://app.test/jobs/7\n"
    browser = Browser([page + "Building", page + "Building", page + "Ready"])
    said, failed = browser.answer("browser_wait_until", {"text": "Ready", "minutes": 5})
    assert not failed and said.startswith("“Ready” shows, after 2 min.")
    reload = ("browser_navigate", {"url": "https://app.test/jobs/7"})
    look = ("browser_snapshot", {})
    assert browser.calls == [look, reload, look, reload, look]


def test_a_wait_never_lasts_past_the_factorys_ceiling() -> None:
    browser = Browser(["- Page URL: https://app.test/jobs/7\nBuilding"] * 3)
    said, failed = browser.answer("browser_wait_until", {"text": "Ready", "minutes": 90}, 30, 1)
    assert not failed
    assert said.startswith(
        "“Ready” did not show in the page's text within 1 min (the most one wait may last is 1 min). "
        "Words drawn in a picture or a live view are no page text: a screenshot shows them."
    )


def test_a_cancelled_wait_stops_at_its_next_reload() -> None:
    browser = Browser(["- Page URL: https://app.test/jobs/7\nBuilding"] * 3)
    browser.ticks.set()
    said, failed = browser.answer("browser_wait_until", {"text": "Ready", "minutes": 5})
    assert (said, failed) == ("the wait was cancelled", True)
    assert browser.calls == [("browser_snapshot", {})]


def test_the_desktop_opens_at_its_own_size() -> None:
    browser = Browser([])
    browser.answer("browser_open_desktop", {"url": "http://127.0.0.1:6080/vnc.html"})
    assert browser.calls == [
        ("browser_resize", {"width": 1280, "height": 800}),
        ("browser_navigate", {"url": "http://127.0.0.1:6080/vnc.html"}),
    ]


@pytest.mark.parametrize(
    ("name", "args", "code", "says"),
    [
        ("browser_color_scheme", {"scheme": "dark"}, "page.emulateMedia({ colorScheme: 'dark' })", "dark colours"),
    ],
)  # fmt: skip
def test_a_tool_that_works_in_the_browser_server_runs_only_its_own_fixed_code(
    name: str, args: dict[str, Any], code: str, says: str
) -> None:
    browser = Browser([])
    said, failed = browser.answer(name, args)
    assert browser.calls == [("browser_run_code_unsafe", {"code": f"async (page) => {{ await {code}; }}"})]
    assert not failed and says in said


def test_a_request_waits_for_its_release_or_its_seconds_and_still_meets_the_browsers_blocks() -> None:
    browser = Browser([], ("held",))
    said, failed = browser.answer("browser_hold_request", {"pattern": '**/x"); y("', "seconds": 2.5})
    [(name, args)] = browser.calls
    assert (
        name == "browser_run_code_unsafe" and 'const p = "**/x\\"); y(\\"";' in args["code"]
    )  # the address stays data
    # fallback, never continue: the held request meets the routes that keep the dashboard out of reach.
    assert "Promise.race([go, page.waitForTimeout(2500)])" in args["code"] and "await route.fallback();" in args["code"]
    assert not failed and "until you release it, or 2.5 s at most" in said


@pytest.mark.parametrize(
    ("name", "result", "says", "failed"),
    [
        ("browser_release_request", "waiting", "The held request to **/x goes on now.", False),
        ("browser_release_request", "armed", "No request to **/x had come yet: the hold is lifted.", False),
        ("browser_release_request", None, "no request to **/x is held: its hold ended, or none was set", True),
        ("browser_hold_request", "busy", "a request to **/x is held already: release it first", True),
    ],
    ids=["a-request-waits", "none-came-yet", "nothing-held", "held-twice"],
)
def test_a_release_says_what_it_let_go(name: str, result: str | None, says: str, failed: bool) -> None:
    assert Browser([], (result,)).answer(name, {"pattern": "**/x", "seconds": 5}) == (says, failed)


def test_typed_keys_hold_shift_for_capitals_and_symbols_and_the_text_never_runs_as_code() -> None:
    browser = Browser([])
    said, failed = browser.answer("browser_type_keys", {"text": 'Hi "x'})
    keys = '[["press", "Shift+KeyH"], ["type", "i"], ["type", " "], ["press", "Shift+Quote"], ["type", "x"]]'
    each = "await page.keyboard[how](key); await page.waitForTimeout(40);"
    code = f"async (page) => {{ for (const [how, key] of {keys}) {{ {each} }} }}"
    assert browser.calls == [("browser_run_code_unsafe", {"code": code})]
    assert not failed and said.startswith("Typed 5 keys. A desktop may draw them seconds late")


def test_a_desktop_click_rests_at_its_point_then_holds_the_button_as_the_desktop_takes_a_click() -> None:
    browser = Browser([])
    said, failed = browser.answer("browser_mouse_press_xy", {"x": 100, "y": 200.5})
    rest = "await page.mouse.move(100, 200.5); await page.waitForTimeout(250);"
    held = "await page.mouse.down(); await page.waitForTimeout(150); await page.mouse.up();"
    code = f"async (page) => {{ {rest} {held} }}"
    assert browser.calls == [("browser_run_code_unsafe", {"code": code})]
    assert (said, failed) == ("Pressed at 100, 200.5. A screenshot shows what it did.", False)


@pytest.mark.parametrize(
    ("name", "args", "says"),
    [
        ("browser_sign_in", {"address": "me"}, "name the test address your brief gives"),
        ("browser_wait_until", {"text": " ", "minutes": 1}, "name the words to wait for"),
        ("browser_wait_until", {"text": "Ready", "minutes": 0}, "above zero"),
        ("browser_wait_until", {"text": "Ready", "minutes": 1}, "the page names no address to reload"),
        ("browser_open_desktop", {"url": "file:///etc/passwd"}, "starting http:// or https://"),
        ("browser_color_scheme", {"scheme": "blue"}, "say light or dark"),
        ("browser_type_keys", {"text": ""}, "give the text to type"),
        ("browser_hold_request", {"pattern": "**/cancel", "seconds": 31}, "at most 30 seconds"),
        ("browser_hold_request", {"pattern": " ", "seconds": 5}, "give the address"),
        ("browser_release_request", {"pattern": " "}, "give the address you held"),
        ("browser_mouse_press_xy", {"x": 1280, "y": 10}, "the point is off the page: x from 0 to 1279"),
        ("browser_mouse_press_xy", {"x": "1); x(", "y": 10}, "give the point as x and y"),
        ("browser_mouse_press_xy", {"x": True, "y": 10}, "give the point as x and y"),
        ("browser_release_request", {"pattern": "**/x"}, "the browser answered with no result"),
    ],
)
def test_a_factory_tool_that_cannot_do_what_was_asked_fails_and_says_why(
    name: str, args: dict[str, Any], says: str
) -> None:
    said, failed = Browser(["Building"] * 3).answer(name, args)
    assert failed and says in said


def test_a_browser_failure_reaches_the_tester_without_the_sign_in_link() -> None:
    def broken(name: str, args: dict[str, Any]) -> str:
        raise ServerError(f"browser_navigate failed: net::ERR_CONNECTION_REFUSED at {args['url']}")

    tools = Tools(broken, lambda address: LINK, lambda link: None, 60, 15, (1280, 800), 40, 30, 150, 250, lambda: 0.0)
    said, failed = tools.answer("browser_sign_in", {"address": "tester-1@example.com"}, threading.Event())
    assert failed and said == "browser_navigate failed: net::ERR_CONNECTION_REFUSED at the sign-in link"
