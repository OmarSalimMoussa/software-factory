from __future__ import annotations

import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from runtime.shared import network


class _Answers(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/hangup":
            return  # the connection closes with no answer
        if self.path == "/cut":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"only part")
            return
        if self.path == "/slow":
            time.sleep(2)
        status = 302 if self.path == "/moved" else int(self.path.strip("/") or 200) if self.path != "/slow" else 200
        self.send_response(status)
        if status == 302:
            self.send_header("Location", "/200")
        self.end_headers()
        self.wfile.write(f"answered {status}".encode())

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Answers)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize(
    ("path", "follow", "status", "body"),
    [("/200", False, 200, b"answered 200"), ("/503", False, 503, b"answered 503"), ("/moved", False, 302, b""),
     ("/moved", True, 200, b"answered 200")],
    ids=["success", "an-error-status-is-an-answer", "a-redirect-is-an-answer", "followed-when-asked"],
)  # fmt: skip
def test_the_door_answers_what_the_host_said(server: str, path: str, follow: bool, status: int, body: bytes) -> None:
    answer = network.call(server + path, 5, follow=follow)

    assert answer.status == status
    assert answer.body.startswith(body)


def test_checked_takes_only_success_and_a_passed_deadline_is_a_timeout(server: str) -> None:
    assert network.call(server + "/200", 5).checked("the host").body == b"answered 200"
    with pytest.raises(network.StatusError, match="the host answered HTTP 503"):
        network.call(server + "/503", 5).checked("the host")
    with pytest.raises(TimeoutError):
        network.call(server + "/slow", 0.3)


def _upgrade(url: str) -> network.Answer:
    asked = urllib.request.Request(url, headers={"Connection": "Upgrade", "Upgrade": "websocket"})
    return network.upgrade(asked, 5)


@pytest.mark.parametrize("door", [lambda url: network.call(url, 5), _upgrade], ids=["call", "upgrade"])
@pytest.mark.parametrize(
    ("path", "kind"), [("/hangup", "RemoteDisconnected"), ("/cut", "IncompleteRead")], ids=["dropped", "cut-short"]
)
def test_a_broken_exchange_fails_naming_what_broke(
    server: str, path: str, kind: str, door: Callable[[str], network.Answer]
) -> None:
    with pytest.raises(OSError) as failed:
        door(server + path)

    assert network.failure_class(failed.value) == kind


def test_an_upgrade_the_host_refuses_is_an_answer(server: str) -> None:
    answer = _upgrade(server + "/426")

    assert (answer.status, answer.body) == (426, b"answered 426")


def test_an_upgrade_goes_only_to_this_machine() -> None:
    with pytest.raises(ValueError, match="not this machine"):
        _upgrade("http://example.test/")


def test_a_request_for_this_machine_never_goes_through_a_proxy(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")  # the discard port: a proxied request would fail
    monkeypatch.setenv("no_proxy", "")

    assert network.call(server + "/200", 5).status == 200
