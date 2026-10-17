# Operating the factory

What the owner does, day to day. The dashboard is the one window. A device signs in to it once, with the 6-digit
code from the authenticator app set up at install; after that, every action is one click.

## Install, upgrade and uninstall

Install the factory from a clone of its repo, with your GitHub CLI (`gh`) signed in:

```
gh repo clone OmarSalimMoussa/software-factory && cd software-factory
sudo python3 runtime/system/install.py --commit HEAD --repo <your checkout of the product>
```

The install seals the commit into a new copy under `/var/lib/software-factory-stage/` (owned by root, read-only,
removed when it ends) and runs every step from it. It prints each phase with a check mark: checking this computer,
preparing it, connecting your accounts, setting up the factory, starting up and finishing.

- **Questions** come as the install reaches them: the 6-digit code's secret as a QR code to scan, with its key to type
  instead; your GitHub token, a read-only token for the factory's repo, and the agents' Claude token. With your `gh`
  signed in, the install puts the GitHub keys in place itself; when it is not, it asks you to sign in, or shows each
  key with where it goes.
- **The log** keeps every detail: each command, its output and how long each step took. `--verbose` prints it too.
  Each install and uninstall writes a new one in `/var/log/software-factory/`, readable by you and root.
- **Options:** `--healthchecks`, `--keep-on-failure` (which keeps the sealed copy too), `--dry-run` (no sudo needed)
  and `--set key=value`.
- **When it stops**, it says in one sentence what failed and what to do. A first install that fails takes back what
  it made, unless you kept it. Run it again after a fix: each step finishes what an earlier run left.

**Upgrade** to any commit of the factory's repo on GitHub with the owner action `factory.upgrade`, given the commit's
full sha. The factory pauses, waits for every agent to settle, and starts root's upgrade unit. The unit downloads the
commit with the read-only token, seals it and runs its install, which asks nothing. No commit need be newer than the
one installed: to go back, upgrade to the commit before. While the dashboard is away, its address shows a page that
says the factory is upgrading and comes back by itself; Home then says how it ended. An upgrade that needs an answer
(a new token, a key to add) stops and says so: install by hand to give it. If the dashboard does not come back, start
the unit for the commit before over SSH: `sudo systemctl start software-upgrade@<sha>.service`.

`sudo python3 runtime/system/uninstall.py [--repo <path>] [--dry-run]` removes the factory. It runs from your clone,
or from `/opt/software-factory`.

## Start

1. Plug the laptop in and run `factory status`: the vitals, the sessions and both environments.
2. Start the orchestrator from the dashboard's Agents page, or with `factory start orchestrator`.

## Every day

- **Home** shows what needs you: up to three proposals, each with a picture and why now. Approve, reorder or say not
  now. Nothing starts until you approve it, except the fix for a clear prod outage.
- **Also needs you** holds the rest: a spec to sign off (open its brief, select text to comment on, press Go or Send
  feedback), a stuck task (Extend, To a designer or Park), questions from sessions, and a task blocked by something
  only you can give, such as a key or an account: give it, then press **Fixed, carry on**. A task waiting for
  another task's check to pass is on hold, and comes back by itself.
- **Boost** any queued task or request on the Work page to make it the next proposal.
- **Done**, on the Work page, lists each finished task and how far it has reached: local, GitHub, staging, prod.
  Its Open button opens slot 0 on this laptop, signed in as the test address you set under Settings, Slots.
- **Talk to the orchestrator** in its chat. What you ask becomes a request in your words. What you say about the
  factory itself goes to `SELF_REFLECT.md`.

## Ship

Nothing reaches GitHub until you push it. When checked work is ready, the Staging card on Home shows **Push N to
staging**: press it to see what goes out (each task, a revert, the factory's upgrade), then Push to staging. The
warden checks it all again, pushes, and CI deploys staging.

The product's CI is the factory's templates, copied in unchanged. It checks every push, and builds each push to the
default branch, smoke-tests it and deploys it to staging, the standard way the product's `factory.toml` describes
([ci.md](ci.md)). The server's deploy command does the rest ([deploy.md](deploy.md)). Staging and prod each answer a
health address with JSON naming the workspace, the running commit as `build_commit`, and the work still running as
`busy`.

When staging looks right, press Push to prod in the top bar, then Push to prod in the panel. The warden starts the
product's `deploy-prod.yml` with exactly the commit staging runs, follows its deploy job, then watches prod until it
answers on it. The release steps run last (a browser extension's store upload, when it changed); a failed release shows
as the push's last step, with prod already live. If prod misbehaves, push back the same way: the warden starts the same
workflow with the commit prod ran when the last push began, so a push that never answered does not move the target. Push
to prod refuses a staging build pinned off the default branch: lift the pin first.

Your own checkout of the product runs the factory's git hooks too: a merge into the default branch there lands only
with a tree that passed `factory check`.

## Errors and traces

Every product gets a database, traces (Langfuse) and errors (GlitchTip) from one copy of the shared services on its
server, for prod, staging and the stack slots alike ([server.md](server.md), [shared-services.md](shared-services.md)).
Each new prod error arrives once as a request in the agents' inbox, for triage beside your feedback. Agents read
errors and traces with `factory errors` and `factory traces`. The factory's own failures report to a GlitchTip
project of its own there, `factory`, which no agent reads.

## The product's tools

The agents and the stack run the product's scripts with your own tools: the install copies each program in
`install.toolchain` from your PATH, and the Python the product asks for (its `.python-version`, else its
`requires-python`) from your uv, into `/opt/software-factory-toolchain/`. The copies are root-owned and read-only, and
the factory's uv finds its Python there and never downloads one. When the product asks for a Python you have not
installed, the install stops and names it: run `uv python install <version>`, then install again. A check may write
the tools' caches in the agent's home (`checks.writable_caches`).

## Agents' sessions

Each session runs with no sandbox, every tool allowed, no permission prompt and no auto-memory: the guard is its one
wall. The factory reads parts of Claude Code that no version promises to keep: its session records, the line
`claude --bg` prints, its transcripts. So the agents' copy is the one the install copied from you, and it never
updates itself. After an install, check that one session starts and the vitals are green. Should sessions ever run
with no record the warden reads, the vitals turn red and the crash watch resumes and retires nothing until they are
seen.

## Pause, stop, resume

- **Pause the factory:** every session, the orchestrator included, commits its work and pauses. Nothing is
  lost; Resume brings everything back in order.
- **Stop an agent** from the Agents page: it retires with everything under it, and its task returns to Proposed with
  its branch kept. Stopping the orchestrator stops the whole factory, so the dashboard asks you to confirm it.

## YOLO mode

Switch on **YOLO mode** in Settings to let the factory run without you, overnight say. While it is on, the warden
approves the orchestrator's proposals as places free, answers any question an agent asks you with "Be proactive and
solve it yourself.", and hands each spec's Go and each stuck task to the orchestrator to decide. Nothing is pushed:
the night's merges wait on Home as Push N to staging. The daily budget, the guard and every other limit still apply.
Home shows a band while it is on; switch it off there or in Settings. Every choice is in its task's log, saying who
made it.

## Switch the Claude account

The agents sign in to Claude with one of your accounts at a time. To move them to another, when one nears its
limits:

- **On the dashboard:** open Settings. Under Claude account, press Switch on the account you want.
- **In a terminal:** run `factory account use <name>`.

The factory gives the agents that account's token, restarts their Claude daemon, and brings one agent back first.
Only its answer proves the token signs in: when no answer comes, or the account is at its plan limit, the factory goes
back to the account before and says why. Then it brings back every other agent that was working or waiting, where it
left off. You see each step as it runs. Until the daemon is back, no agent starts; while a merge is in its last turn,
or another switch runs, a switch waits. The active account is the one whose token the agents' Claude holds.

Add an account with Add and switch on the same page, which switches to it at once, or with
`factory account add <name>`, which asks for the token
that `claude setup-token` prints without showing it. `factory account list` shows every account and marks the active
one. `factory account remove <name>` forgets an account the agents do not use. The factory never shows a token again,
only its last four characters. Only you can do any of this: these commands reach the warden on the dashboard's own
socket, which answers your user alone.

The install asks for the first account's token and a name for it (`main` if you press Enter). An install from before
accounts keeps its one token as the account `main`.

## Tune

Change any setting on the Settings page and save. Each value is checked before it is saved, the save is written to the
warden's state on this machine, and only what reads that value reloads. See [settings.md](settings.md).

## Improve the factory

The factory never changes itself. Read the Self-reflect page: misses, friction, discoveries, misroutes and incidents.
To act on one:

1. Change the factory's repo by hand: branch, test, merge, push. Run the self-check (`pytest -m self_check`) and the
   browser drives (`node tests/dashboard_ui/run.cjs`) before you push.
2. Upgrade to the pushed commit with `factory.upgrade`. The upgrade also commits the product's `.github/factory-ref`
   and CI workflows for the new factory; that commit waits on Home for your Push to staging, like any other.
3. Run the server set-up again from a factory checkout at that commit (`python3 -m standard.server.setup --product
   <path>`). It refuses a checkout at another commit than `.github/factory-ref` names. Older builds stay deployable.
4. **Restart the factory** on Home: every agent comes back with its work. One whose subagents the upgrade changed
   comes back as a copy that has the new ones, with its whole conversation; its chat link changes.

## When something is wrong

- **Prod down:** a red band on Home. The factory starts one fix without you and ships it to staging; you push it to
  prod, or push back. Stop the fix from the band if you prefer to handle it yourself.
- **The warden unreachable:** the dashboard keeps its last known state, marked old, and refuses every change until
  the warden answers. Check its service on the laptop.
- **Vitals red:** nothing starts until memory, CPU or power recover, or the install hash matches an upgrade.
