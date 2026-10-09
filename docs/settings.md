# Settings

Every value the factory uses is a setting: caps, models, rounds, times and thresholds. None is written in code.

## Two layers

- **Defaults** are `settings.toml` in the factory's repo, installed with it.
- **The owner's values** are saved from the dashboard's Settings page, with the 6-digit code. Nothing changes until
  Save. Each save is checked first: the type, the range, and for a model, the allowed list. A bad value is refused
  with the reason, and nothing is written.
- **A save** is applied at once, kept in the warden's state, and committed to `settings.toml` in the factory's repo,
  so the repo stays the record. No agent and no operation stops. A reinstall folds the saved values in.
- **Every change is audited** by its commit. Reset to default puts one key back.

## The groups

| Group | Holds |
| --- | --- |
| `repo` | The product's checkout, default branch, GitHub name, the staging and prod health URLs, the owner's login |
| `models` | The allowed models and efforts, and the model and effort of every role |
| `caps` | Implementers, designers and subagents at once |
| `subagents` | Which subagent kinds each role may start |
| `rounds` | QA rounds, critic rounds, check runs, and how far one extension goes |
| `qa` | Time per item, items per brief, desktop time, widths, evidence days |
| `intake` | Batch size and wait, the fast lane's time, the score's weights |
| `routing` | Slices per rung, open proposals at once, the estimate's window, tries to confirm a cause |
| `slots` | How many, the first port, the output lines an agent sees |
| `budget` | The slice's memory and CPU, and the memory a test run needs |
| `vitals`, `watch` | Amber and red levels; probe, loop, grace and silence times |
| `cost`, `prices` | The daily budget, the plan-limit levels, and dollars per million tokens per model |
| `sandbox` | The hosts a session may reach |
| `dashboard` | Port, refresh and stale times, the code lock, page size, the hosts it answers |
| `gc` | The nightly prune's hour, how long images stay, the Docker disk budget |

`models.allowed` and `prices` are set at install and are read-only on the dashboard.

## When a change takes effect

| Setting | Takes effect |
| --- | --- |
| Caps, rounds, QA, vitals, watch, intake and routing values | At once: every guard call and warden pass reads them fresh |
| A session's or a subagent's model and effort | At its next start |
| The slice budget | Within a minute |
| The slot count | At the next slot claim |
| The repo | At the next orchestrator start; stop the old one first |
| The dashboard's port, hosts and threads | At the next warden start |

The dashboard shows the value running now beside one that waits, such as "4 at the next slot claim".
