"""A stand-in for Playwright's MCP server, for the wrapper's tests: it answers on standard input and output as the
server does, and writes each request it gets to the file named first. With FAIL_TRACE set, the trace never starts;
with ASK_ROOTS set, it asks the session for its roots before it starts the trace and waits for the answer, as
Playwright does; a call to browser_crash ends it with exit 3.

Run: python3 fake_mcp.py <log file>
"""

from __future__ import annotations

import json
import os
import sys

LAST = ["about:blank"]  # the page the browser is on
TOOLS = [
    "browser_navigate",
    "browser_snapshot",
    "browser_resize",
    "browser_close",
    "browser_start_tracing",
    "browser_stop_tracing",
]


def answer(message: dict[str, object]) -> dict[str, object] | None:
    method, params = message.get("method"), message.get("params") or {}
    assert isinstance(params, dict)
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
    if method == "tools/list":
        return {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in TOOLS]}
    if method != "tools/call":
        return None
    name, args = params.get("name"), params.get("arguments") or {}
    assert isinstance(args, dict)
    if name == "browser_start_tracing" and os.environ.get("FAIL_TRACE"):
        return {"content": [{"type": "text", "text": "### Error\ntracing is off"}], "isError": True}
    if name == "browser_navigate":
        LAST[0] = str(args["url"])
    said = f"### Page\n- Page URL: {LAST[0]}\n" if name in ("browser_navigate", "browser_snapshot") else f"{name} done"
    return {"content": [{"type": "text", "text": said}]}


ROOTS = "roots-1"
lines, held = iter(sys.stdin), []
with open(sys.argv[1], "a", encoding="utf-8") as log:
    while held or (line := next(lines, None)) is not None:
        message = json.loads(held.pop(0) if held else line)
        params = message.get("params") or {}
        log.write(f"{message.get('method')} {params.get('name', '')}".strip() + "\n")
        log.flush()
        if params.get("name") == "browser_crash":
            sys.exit(3)
        if params.get("name") == "browser_start_tracing" and os.environ.get("ASK_ROOTS"):
            print(json.dumps({"jsonrpc": "2.0", "id": ROOTS, "method": "roots/list"}), flush=True)
            for waited in lines:
                if json.loads(waited).get("id") == ROOTS:
                    break
                held.append(waited)
        result = answer(message)
        if "id" in message and result is not None:
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
