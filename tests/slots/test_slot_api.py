"""The slot-api tool reaches its own slot's port on the loopback and nothing else."""

from __future__ import annotations

import io
import json

import pytest

from runtime.slots import slot_api
from runtime.slots.slot_api import SlotApiError, build, serve


@pytest.mark.parametrize(
    "arguments",
    [
        {"method": "GET", "path": "http://evil.example/"},
        {"method": "GET", "path": "//evil.example/x"},
        {"method": "GET", "path": "/a b"},
        {"method": "GET", "path": "@evil.example/"},
        {"method": "GET", "path": "/", "headers": {"Host": "evil.example"}},
        {"method": "GET", "path": "/", "headers": {"X-A": "1\r\nX-B: 2"}},
        {"method": "GET", "path": "/", "headers": {"X-Name": "\u540d\u524d"}},
        {"method": "CONNECT", "path": "/"},
    ],
)
def test_a_request_that_could_leave_the_slot_is_not_sent(arguments: dict[str, object]) -> None:
    with pytest.raises(SlotApiError):
        build(8101, arguments)


def test_a_request_goes_to_the_slots_own_port() -> None:
    request = build(8102, {"method": "POST", "path": "/api/items?x=1", "body": "{}"})
    assert (request.full_url, request.get_method(), request.data) == (
        "http://127.0.0.1:8102/api/items?x=1",
        "POST",
        b"{}",
    )


def test_the_server_speaks_mcp_over_stdio() -> None:
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "slot_api", "arguments": {"path": "/"}}},
    ]
    out = io.StringIO()
    serve(io.StringIO("".join(json.dumps(m) + "\n" for m in lines)), out, lambda a: build(8101, a).full_url)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[1]["result"]["tools"][0]["name"] == "slot_api"
    assert replies[2]["result"]["isError"] is True  # no method named


def test_each_request_goes_to_the_slot_the_caller_holds_at_that_moment(monkeypatch: pytest.MonkeyPatch) -> None:
    held = {"ok": True, "timeout_seconds": 30, "max_kib": 256}
    answers = iter([{"ok": False, "reason": "claim a slot first"}, {**held, "port": 8101}, {**held, "port": 8102}])
    monkeypatch.setattr(slot_api, "send", lambda request, timeout, limit: request.full_url)
    call = slot_api.caller(lambda: next(answers))

    with pytest.raises(SlotApiError, match="claim a slot first"):
        call({"method": "GET", "path": "/"})
    assert [call({"method": "GET", "path": "/"}) for _ in range(2)] == [
        "http://127.0.0.1:8101/",
        "http://127.0.0.1:8102/",
    ]
