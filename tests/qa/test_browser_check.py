"""The install's check of a tester's browser passes only once a page opened and closed, and leaves no run behind."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from queue_kit import World

from runtime.qa import browser_check

# A stand-in for Playwright's MCP server: it answers each request by its mode.
SERVER = """
import json, sys

mode = sys.argv[1]
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    if mode == "dies":
        sys.exit(1)
    if mode == "odd":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": []}), flush=True)
        continue
    name = message["params"].get("name")
    result = {"content": [{"type": "text", "text": "### Ran Playwright code"}]}
    if mode == "broken" and name == "browser_navigate":
        result = {"content": [{"type": "text", "text": "### Error\\nchrome is missing"}], "isError": True}
    print("a log line that is no answer", flush=True)
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


@pytest.mark.parametrize(
    ("mode", "wrong"),
    [
        ("opens", ""),
        ("broken", "browser_navigate failed: ### Error chrome is missing"),
        ("dies", "the browser server did not answer the handshake"),
        ("odd", 'the handshake failed: "an answer with no result"'),
    ],
)
def test_the_check_passes_only_once_a_page_opens_and_closes(
    world: World, tmp_path: Path, mode: str, wrong: str
) -> None:
    server = tmp_path / "server.py"
    server.write_text(SERVER)
    runs = tmp_path / "runs"

    found = browser_check.check("qa-prod", runs, world.settings, datetime.now(UTC),
                                lambda config: [sys.executable, str(server), mode])  # fmt: skip

    assert found == wrong
    assert not any(runs.iterdir())
