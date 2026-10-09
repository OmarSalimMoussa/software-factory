from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from runtime.warden.probes import Answer, NetworkFailure, http_get


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        status = int(self.path.strip("/"))
        self.send_response(status)
        if status == 302:
            self.send_header("Location", "/200")
        self.end_headers()
        self.wfile.write(b'{"build_commit": "abcdef1"}')

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("status", [200, 302, 503])
def test_the_host_answer_is_kept_as_is_and_a_redirect_is_not_followed(server: str, status: int) -> None:
    answer = http_get(f"{server}/{status}", 5)
    assert answer.status == status
    assert isinstance(answer, Answer)


def test_a_refused_connection_is_a_network_failure_not_a_timeout() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(NetworkFailure):
        http_get(f"http://127.0.0.1:{port}/health", 5)
