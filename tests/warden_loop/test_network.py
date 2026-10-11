from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from runtime.shared import network


class _Answers(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
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


def test_a_request_for_this_machine_never_goes_through_a_proxy(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")  # the discard port: a proxied request would fail
    monkeypatch.setenv("no_proxy", "")

    assert network.call(server + "/200", 5).status == 200
