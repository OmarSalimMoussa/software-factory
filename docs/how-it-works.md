# How it works

The factory in plain words: who does the work, how much work a task gets, the parts that hold it together, and the
limits it keeps. [dev-cycle.md](dev-cycle.md) gives every rule of each step a change takes.

## The agents

Each agent is a Claude Code session with one role.

| Agent | Its job |
| --- | --- |
| Orchestrator | The lead. It proposes and plans the work, approves specs at rung 3, and merges branches. |
| Intake | Turns an urgent request into a task within minutes. |
| Triage | Sorts batches of requests into tasks. Each request becomes a new task, joins a task, is dropped as noise, or waits. |
| Designer | Writes the spec for a bigger change. |
| Critics | Review a spec until a round finds nothing new. Each reviews one area: product, user experience (UX), security, reliability or architecture. |
| Implementer | Writes the code and its tests. For a bug, it shows the bug happening before it fixes it. |
| Testers | Use the running app to find problems, each in one area. Three check the screens: do they work, are they easy to use, do they look right. Others check the API (how other programs talk to your app), security, old features that must keep working, and production after each deploy. |

Each role's instructions are Markdown files in [plugin/](../plugin/). You choose each role's model and effort on the
Settings page.

## How much work a task gets

The orchestrator plans each task on the lowest "rung" that can do it. A higher rung costs more and asks more of you.

| Rung | Who works on it | When |
| --- | --- | --- |
| 0 | Nobody | No code needs to change. The task closes with a reason. |
| 1 | A small implementer (Sonnet by default) | The cause is known down to the file and line. |
| 2 | A large implementer (Opus by default) | The request is clear and contained. |
| 3 | A designer, then an implementer | The change is bigger or less clear. The orchestrator approves the spec. |
| 4 | A designer, then you, then an implementer | A large change people will see, or a large or costly backend change. You approve the spec. |

When a task turns out to need a higher rung, it comes back to you as a new proposal.

## The parts

| Part | What it does |
| --- | --- |
| Warden | A system service, and the only way an agent session starts. It enforces the limits: how many agents run, what they cost, and how much time and memory they use. Everything the dashboard shows comes from it. |
| Guard | A hook: a script Claude Code runs before every tool call of every agent. It blocks what an agent may not do, and says what to do instead. |
| `factory` command | How agents and you ask the warden for things, such as `factory status` or `factory check`. |
| Dashboard | Your one window, in a browser. It shows proposals to approve, staging and production, the work, the agents, spend and settings. |
| Checks | One standard set of code checks that every product must pass. A product gives values to them, never checks of its own. |
| Slots | Private copies of your app, one for each implementer. Slot 0 is staging. |
| Build and deploy | Builds and deploys production on the server. Every product also gets a shared database (Postgres) and error tracker (GlitchTip). |
| MCP server | Lets Claude Code on your laptop read the factory and act on it. MCP (Model Context Protocol) is how Claude connects to tools. |

## What stays yours

- **The goal.** You write `GOAL.md`: who the product is for, your goal and its ranked items, how the product looks,
  and how it speaks. No agent may change it.
- **What starts.** Every run needs your approval, unless you turn on YOLO mode.
- **Production.** No agent can push to production. Only you can, from the dashboard.
- **Protected files.** Agents can't change `GOAL.md`, `factory.toml`, the GitHub workflows and a few other paths.
  You can protect more paths with the `repo.protected_paths` setting.

**YOLO mode** lets the factory run without you, overnight for example. The warden approves proposals as places
free up, and the orchestrator answers the agents' questions. Nothing goes to production: in the morning, the night's
work waits on staging for you.

## Limits

- **Cost.** You set a daily budget. Past it, only urgent work starts that day. Near your Claude plan's weekly limit,
  the factory pauses and waits for you.
- **Machine load.** The warden keeps processor, memory and disk use under set levels, so the agents never starve
  production.
- **Checks.** A branch merges only when its exact code passed `factory check`.
- **Testers.** Every required tester must pass on the final version of a branch.

Every limit is a setting with a default in [settings.toml](../settings.toml). You change them on the Settings page.
[settings.md](settings.md) describes each group.

## What you need

- A server running Ubuntu 24.04, with a domain name for your product and one for the dashboard.
- Docker and Tailscale, a private network between your devices. The server set-up installs both on a new server.
- In your own account on the server: Claude Code (`claude`), uv (a Python tool), and the GitHub command-line tool
  (`gh`), signed in.
- A Claude account for the agents. The install asks for its token, from `claude setup-token`.
- An authenticator app, for the dashboard's 6-digit sign-in code.
- Your product's repo on GitHub, in the shape [repo-shape.md](repo-shape.md) describes.

## Improve the factory

The factory never changes its own code. Agents write down what went wrong on the **Self-reflect** page: misses,
friction, and tasks planned on the wrong rung. You, or Claude, read it and change the factory's repo. Then install
the new commit with the `factory.upgrade` action. To go back, install the commit before it.

The factory's own code is Python 3.12's standard library, with no dependencies. Run its tests with:

```bash
uv run --python 3.12 --extra dev pytest
```

[CLAUDE.md](../CLAUDE.md) holds the rules for changing the factory.
