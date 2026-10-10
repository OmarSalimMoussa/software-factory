from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from loop_fakes import World, healthy

from runtime.warden import probes
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


@pytest.mark.parametrize(
    ("factory_toml", "asked", "error"),
    [
        (None, ["https://staging.notes.example.com/healthz"], ""),
        ("", [], "no health address: factory.toml is missing"),
        ("[product]\nname = 'Notes!'\n", [], "no health address: factory.toml is not usable: "),
    ],
    ids=["from-the-checkout", "missing", "broken"],
)
def test_the_health_address_comes_from_the_checkouts_factory_toml_and_is_never_guessed(
    world: World, factory_toml: str | None, asked: list[str], error: str
) -> None:
    path = world.checkout / "factory.toml"
    if factory_toml == "":
        path.unlink()
    elif factory_toml is not None:
        path.write_text(factory_toml)
    urls: list[str] = []

    def fetch(url: str, timeout: float) -> Answer:
        urls.append(url)
        return healthy("a" * 40)

    probe = probes.take(world.ctx, probes.STAGING, fetch)
    assert urls == asked
    assert probe.ok == (not error) and not probe.counts and probe.error.startswith(error)
