"""The factory's MCP at /mcp, over HTTP as Claude Code's client reaches it: one JSON-RPC message in each POST, with a
signed-in device's token as the bearer token."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest
from dashboard_rig import Rig
from dashboard_seed import TEST_TOKEN

from runtime.dashboard.server import serve
from runtime.shared import store

TOKEN = {"Authorization": f"Bearer {TEST_TOKEN}"}
APPROVE = {"verb": "proposal.approve", "args": {"task": "t-0027", "idempotency_key": "mcp-approve-1"}}


def _tool(name: str, arguments: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


class Client:
    def __init__(self, url: str) -> None:
        self.url = url

    def send(self, body: Any, headers: dict[str, str] = TOKEN, method: str = "POST") -> tuple[int, Any]:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        sent = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers}
        request = urllib.request.Request(self.url, raw if method == "POST" else None, sent, method=method)
        try:
            with urllib.request.urlopen(request) as answer:
                status, text = answer.status, answer.read()
        except urllib.error.HTTPError as refused:
            status, text = refused.code, refused.read()
        return status, json.loads(text) if text else None

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        status, answer = self.send({"jsonrpc": "2.0", "id": "r-1", "method": method, "params": params})
        assert status == 200 and answer["id"] == "r-1", answer
        return dict(answer)

    def tool(self, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
        status, answer = self.send(_tool(name, arguments))
        assert status == 200, answer
        return answer["result"]["isError"], json.loads(answer["result"]["content"][0]["text"])


@pytest.fixture
def mcp(rig: Rig) -> Iterator[Client]:
    server = serve(rig.app, 0, 2, 5)
    try:
        yield Client(f"http://127.0.0.1:{server.server_address[1]}/mcp")
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Bearer not-a-device"}, 401),
        ({"Cookie": f"owner={TEST_TOKEN}"}, 401),
        ({**TOKEN, "Origin": "https://attacker.example"}, 403),
        ({**TOKEN, "Host": "attacker.example"}, 403),
    ],
)
def test_a_request_without_a_device_token_or_from_elsewhere_runs_nothing(
    rig: Rig, mcp: Client, headers: dict[str, str], status: int
) -> None:
    before = rig.places.proposals.read_text()
    answer = mcp.send(_tool("call", APPROVE), headers)
    assert answer[0] == status and "result" not in answer[1]
    assert rig.places.proposals.read_text() == before


def test_claude_codes_handshake_gets_the_answers_it_expects(mcp: Client) -> None:
    assert mcp.rpc("server/discover", {})["error"]["code"] == -32601  # Claude Code then falls back to initialize
    for asked, offered in (("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"), ("2099-01-01", "2025-11-25")):
        result = mcp.rpc("initialize", {"protocolVersion": asked, "capabilities": {}})["result"]
        assert (result["protocolVersion"], result["capabilities"]) == (offered, {"tools": {}})
    assert mcp.send({"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)
    assert mcp.send(b"", method="GET")[0] == 405
    tools = {tool["name"]: tool for tool in mcp.rpc("tools/list", {})["result"]["tools"]}
    verbs = tools["call"]["inputSchema"]["properties"]["verb"]["enum"]
    assert list(tools) == ["snapshot", "call"] and "factory.pause" in verbs and "tick" not in verbs
    assert mcp.rpc("ping", {})["result"] == {}


def test_snapshot_reads_what_the_dashboard_reads(mcp: Client) -> None:
    wanted = ["factory", "sessions", "queue", "settings", "reflections"]
    failed, answer = mcp.tool("snapshot", {"want": wanted})
    assert not failed and answer["ok"] and sorted(answer["state"]) == sorted(wanted) and answer["errors"] == {}
    failed, names = mcp.tool("snapshot", {"want": []})
    assert failed and "reflections" in names["reason"]


def test_call_runs_an_owner_verb_with_the_owners_rights_and_returns_the_wardens_answer(rig: Rig, mcp: Client) -> None:
    failed, answer = mcp.tool("call", APPROVE)
    assert not failed and answer["ok"]
    assert store.read_json(rig.places.proposals)["approved"] == ["t-0027"]
    failed, answer = mcp.tool("call", {"verb": "account.add", "args": {"name": "spare"}, "data": "sk-ant-oat01-spare"})
    assert not failed
    listed = mcp.tool("call", {"verb": "account.list", "args": {}})[1]["accounts"]
    assert "spare" in [account["name"] for account in listed]
    failed, answer = mcp.tool("call", {"verb": "tick", "args": {}})
    assert failed and answer["ok"] is False and answer["reason"]


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (_tool("shell", {"command": "ls"}), 200, -32602),
        ({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, 200, -32601),
        (b"{not json", 400, -32700),
    ],
)
def test_an_unknown_tool_or_method_or_a_body_that_is_not_json_is_a_json_rpc_error(
    mcp: Client, body: Any, status: int, code: int
) -> None:
    answer = mcp.send(body)
    assert answer[0] == status and answer[1]["error"]["code"] == code


def test_a_tool_the_warden_does_not_answer_says_so_and_is_logged(
    rig: Rig, mcp: Client, caplog: pytest.LogCaptureFixture
) -> None:
    assert not mcp.tool("snapshot", {"want": ["factory"]})[0]  # the warden confirms the token while it answers
    rig.warden.stop()
    with caplog.at_level(logging.WARNING, logger="factory.dashboard"):
        status, answer = mcp.send(_tool("call", {"verb": "factory.pause", "args": {}}))
    assert status == 200 and answer["result"]["isError"]
    assert "did not answer" in answer["result"]["content"][0]["text"]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "dashboard: the warden does not answer on its owner socket (WardenDown)"
    ]
