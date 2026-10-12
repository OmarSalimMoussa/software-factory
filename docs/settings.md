# Settings

Every value the factory uses is a setting: caps, models, rounds, times and thresholds. None is written in code.

## Two layers

- **Defaults** are `settings.toml` in the factory's repo, installed with it.
- **The owner's values** are saved from the dashboard's Settings page, with the 6-digit code. Nothing changes until
  Save. Each save is checked first: the type, the range, and for a model, the allowed list. A bad value is refused
  with the reason, and nothing is written.
- **A save** is applied at once and kept in the warden's state, on this machine only: the factory never writes its
  own repo, so one owner's values never become another's defaults. No agent and no operation stops. A reinstall
  keeps the saved values, and drops one for a setting the new release no longer has.
- **Every change is audited** with its old and new value. Reset to default puts one key back.

## The groups

| Group | Holds |
| --- | --- |
| `repo` | The product's checkout, default branch, GitHub name and the paths only the owner changes |
| `models` | The allowed models and efforts, and the model and effort of every role |
| `caps` | Implementers, designers and subagents at once, and the browsers and remote desktops testers run at once |
| `subagents` | Which subagent kinds each role may start |
| `rounds` | QA rounds, critic rounds, check runs, and how far one extension goes |
| `qa` | Time per item, items per brief, desktop time, widths, evidence days, the desktop tester's screen, how a wait reloads and how long it may last |
| `replay` | How a tester's run is replayed for the owner: the builder's timings, how long the picture stays hidden after a sign-in link is typed, and the player's pace |
| `intake` | Batch size and wait, the fast lane's time, the score's weights |
| `routing` | Slices per rung, open proposals at once, the estimate's window, tries to confirm a cause |
| `slots` | How many, the first port, the output lines an agent sees |
| `budget` | The slice's memory and CPU, and the memory a test run needs |
| `vitals`, `watch` | Amber and red levels; probe, loop, grace and silence times |
| `errors` | How often the warden reads prod's new GlitchTip issues into requests, and how many pages one read covers |
| `reports` | How often the warden asks the product for users' new reports |
| `telemetry` | What `factory errors` and `factory traces` give an agent: how many items, how large, how long a call |
| `cost`, `prices` | The daily budget (past it, only severe work starts that day), the plan-limit levels, and dollars per million tokens per model |
| `sandbox` | The hosts a session may reach, and the tool caches in the agent's home it may write |
| `dashboard` | Port, refresh and stale times, the code lock, page size, the hosts it answers |
| `gc` | The nightly prune's hour, how long images stay, the Docker disk budget |
| `install` | The drills' model, time and how many run at once, the guard's timeout, the check-ins' grace, the product's toolchain, how long publishing the dashboard may wait |

`models.allowed` and `prices` are set at install and are read-only on the dashboard. Staging's and prod's health
addresses are not settings: the warden reads them from `factory.toml` in the product's checkout at every probe.

`install.toolchain` names the programs the product's scripts need, such as `uv` and `node`. The install copies each
one from the owner's PATH, so the agents and the stack run the same tools as the owner.

`sandbox.writable_caches` names the folders in the agent's home where sessions may write, besides their own
worktree: the caches of the tools the product's tests run, such as `~/.cache/uv` and `~/.npm`. The setting names
each folder from the home folder down, without the `~/`. The install makes each folder for the agent user. A cache cannot lie in `.claude`, `.config`, `.local`, `.ssh` or `bin`, because a session
that wrote there could act outside its sandbox.

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
| The sandbox's writable caches and the `install` group | At the next install |
| The dashboard's port and hosts, the probe period and the loop period | Only with the install's `--set`: the install wires them into the firewall, the tailnet's link and the check-in periods, so the Settings page shows them read-only |

The dashboard shows the value running now beside one that waits, such as "4 at the next slot claim".
