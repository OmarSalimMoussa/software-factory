# Claude's MCP

The dashboard serves an MCP at `/mcp`. Through it, Claude reads the factory and acts on it as you do on the page,
from the box or from your laptop: `https://factory.example.com/mcp`. It answers only a request that carries a
signed-in device's token.

## Get a token, once

The token comes from the same 6-digit sign-in as the dashboard. Run this in a terminal and type the code your
authenticator app shows:

```
read -p 'Code: ' code
curl -s -D - -o /dev/null https://factory.example.com/api/device \
  -H 'Origin: https://factory.example.com' -H 'Content-Type: application/json' -H 'X-Software: 1' \
  -d "{\"code\": \"$code\", \"idempotency_key\": \"mcp-$(date +%s)\"}" | grep -io 'owner=[^;]*' | cut -d= -f2-
```

It prints the token. If it prints nothing, the code was wrong or already used: wait for the next one and run it
again. Wrong codes lock signing in for a while (`dashboard.wrong_codes_to_lock`, `dashboard.lock_minutes`).

The token works for `dashboard.device_days` days, a year. Then get a new one the same way and add it again. Anyone
who holds it can do all that you can do on the dashboard, so keep it out of chats and repos.

## Add it to Claude Code

```
claude mcp add --transport http factory https://factory.example.com/mcp --header "Authorization: Bearer <token>"
```

Add `--scope user` to have it in every folder, not only the one you run it in. `claude mcp list` then shows
`factory: … ✔ Connected`. A wrong or expired token shows `HTTP 401` there instead.

## What Claude can do

Two tools:

- **`snapshot`** reads what the dashboard reads: the factory's state, the agents, the queue, the settings,
  Self-reflect and more. `want` names the parts; an empty `want` gets the warden to list every name. `time` tells
  where the tasks that reached staging in the last `dashboard.time_days` days spent their time, in seconds: wall
  time per step and per status apart from runtime, and the merges' wait for their check; add `tasks` to read others.
- **`call`** runs any action you have on the dashboard: a verb the warden answers on the owner socket
  (`runtime/warden/verbs.py`), with its `args`. The answer is the warden's own, as JSON, and a refusal says why.
  An `idempotency_key` in `args` makes a retry run the action only once.

The MCP stops while the dashboard does, during an install. Claude waits for it to come back.

## Who it lets in

- Only the dashboard's own addresses (`dashboard.hosts`), so `factory.example.com` is one of them.
- Only a request with a device's token as `Authorization: Bearer <token>`. Anything else gets `401` and nothing
  more. The owner cookie does not open the MCP.
- Claude Code sends no `Origin`, so the MCP refuses only an `Origin` from another site. That is safe because a
  browser never adds a bearer token to a request by itself, as it does a cookie.
- It speaks MCP over plain HTTP posts: one JSON-RPC message in each, answered in the response, with no session and no
  event stream.
