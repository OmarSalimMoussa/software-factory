# Settings

Every value the factory uses is a setting: caps, models, rounds, times and thresholds. None is written in code.

## Two layers

- **Defaults** are `settings.toml` in the factory's repo, installed with it.
- **The owner's values** are saved from the dashboard's Settings page. Nothing changes until
  Save. Each save is checked first: the type, the range, and for a model, the allowed list. A bad value is refused
  with the reason, and nothing is written.
- **A save** is applied at once and kept in the warden's state, on this machine only: the factory never writes its
  own repo, so one owner's values never become another's defaults. No agent and no operation stops. A reinstall
  keeps the saved values, and drops one for a setting the new factory no longer has.
- **Every change is audited** with its old and new value. Reset to default puts one key back.

## The groups

| Group | Holds |
| --- | --- |
| `repo` | The product's checkout, default branch, GitHub name and the paths only the owner changes |
| `models` | The allowed models and efforts, and the model and effort of every role |
| `caps` | Implementers, designers and subagents at once, and the browsers and remote desktops testers run at once |
| `subagents` | Which subagent kinds each role may start |
| `rounds` | QA rounds, critic rounds, check runs, and how far one extension goes |
| `qa` | Time per item and to start, items per brief, desktop time, widths, evidence days, the desktop tester's screen, how a wait reloads and how long it may last |
| `replay` | How a tester's run is replayed for the owner: the builder's timings, how long the picture stays hidden after a sign-in link is typed, and the player's pace |
| `intake` | Batch size and wait, the fast lane's time, the score's weights |
| `routing` | Slices per rung, open proposals at once, the estimate's window, tries to confirm a cause |
| `slots` | How many, the first port, the output an agent sees, how long a run may take, their share of the CPU against prod's, the keys slots take from your env file, staging's address on the internet |
| `budget` | The slice's memory and CPU, the machine's CPU budget, and the memory a test run needs |
| `checks` | How long a check and one test may run, how much of a report an agent sees, and the tool caches a check may write |
| `vitals`, `watch` | Amber and red levels; probe, loop, grace and silence times |
| `errors` | How often the warden reads prod's and staging's new GlitchTip issues into requests, and how many pages of a GlitchTip list one read covers |
| `reports` | How often the warden reads prod's report export, how long it waits for it, and the largest it takes |
| `telemetry` | What `factory errors`, `factory logs` and `factory traces` give an agent: how many items, how large, how long a call |
| `cost`, `prices` | The daily budget (past it, only severe work starts that day), the plan-limit levels, and dollars per million tokens per model |
| `dashboard` | Port, refresh and stale times, the code lock, page size, the hosts it answers |
| `gc` | The nightly prune's hour, how long images stay, the Docker disk budget |
| `install` | The guard's timeout, the check-ins' grace, the product's toolchain |

`models.allowed` and `prices` are set at install and are read-only on the dashboard. Prod's health address is not a
setting: the warden reads it from `factory.toml` in the product's checkout at every probe. Staging's is slot 0's
port on loopback, with the same path.

`slots.zero_origin` is where staging, which is slot 0, opens to the internet: `http://<the box's IPv4 address>:<port>`,
on a port clear of the dashboard's and the slots'. The install puts a site for it on the box's edge, listening on that
address alone, and opens the port in ufw. Slot 0's product is told that address, and Open on the Done tab takes a
sign-in link only there. Empty, staging stays on the box's loopback, and the install takes the site and the port off.

`install.toolchain` names the programs the product's scripts need, such as `uv` and `node`. The install copies each
one from the owner's PATH, so the agents and the stack run the same tools as the owner.

`checks.writable_caches` names the folders in the agent's home a check may write, besides the checkout it checks:
the caches of the tools the product's tests run, such as `~/.cache/uv` and `~/.npm`. The setting names each folder
from the home folder down, without the `~/`. A check writes a cache only once the agents' tools have made it. A cache
cannot lie in `.claude`, `.config`, `.local`, `.ssh` or `bin`, because a check that wrote there could act outside its
walls.

## When a change takes effect

| Setting | Takes effect |
| --- | --- |
| Caps, rounds, QA, vitals, watch, errors, reports, telemetry, intake and routing values, but those below | At once: every guard call and warden pass reads them fresh |
| The replay's timings, and the QA browser's screen and waits | At the next replay built, and at the next tester's start; the player's pace at the next page load |
| A session's or a subagent's model and effort | At its next start |
| The slice budget | Within a minute |
| The slot count | At the next slot claim |
| The repo | At the next orchestrator start; stop the old one first |
| The dashboard's threads, and the warden's (`factory.warden_threads`) | At the next warden start |
| The `install` group | At the next install |
| The dashboard's port and hosts, staging's address, the probe period, the loop period and a push to prod's time | Only with the install's `--set`: the install wires them into the firewall, the edge's sites, the check-in periods and the deploy unit, so the Settings page shows them read-only |

The dashboard shows the value running now beside one that waits, such as "4 at the next slot claim".
