# Operating the factory

What the owner does, day to day. The dashboard is the one window. A device signs in to it once, with the 6-digit
code from the authenticator app set up at install; after that, every action is one click.

## Install, upgrade and uninstall

Install the factory, or update it, from its latest release. With your GitHub CLI (`gh`) signed in, in any folder:

```
gh release download --repo OmarSalimMoussa/software-factory --pattern 'software-factory.pyz*' --clobber
sha256sum -c software-factory.pyz.sha256 && sudo python3 software-factory.pyz --repo <your checkout of the product>
```

The first line downloads the release file and its checksum; the second checks the download, then runs it. The
factory's CI publishes a release only for a commit on which every check passed, so nothing is tested on this
computer. `python3 software-factory.pyz --version` says which commit a file holds; `--check` checks it against its
own manifest. Neither needs sudo.

Run with sudo, the file checks itself, unpacks the factory into a new sealed copy under
`/var/lib/software-factory-stage/` (owned by root, read-only, removed when it ends) and opens the install console in
your browser. It prints the page's link too; the link works once, in the browser that opens it.

- **This laptop** says whether the factory is installed, at which commit and since when, how the last safety checks
  did, whether the warden, the dashboard, GlitchTip, Langfuse and the factory's own GlitchTip answer, and where the
  dashboard is on your tailnet.
- **Install** installs the release's commit. On a laptop that has the factory, it says **Upgrade**, from which
  commit, and what it keeps: your settings, accounts, keys and code, the Healthchecks.io checks, the agents' work, the
  shared services and the factory's own GlitchTip. Turn on **Dry run** to go through every step on this computer
  without changing anything.
- **An install** shows a few phases with a progress bar and one line on what it is doing: checking this computer,
  preparing it, fetching the services, connecting your accounts, setting up the factory, starting up, running
  the safety checks and finishing. It never shows the commands it runs.
- **Questions** appear as the install reaches them: the 6-digit code's secret as a QR code to scan, with its key to
  type instead and a Copy button; your GitHub token and the agents' Claude token, typed into password fields. With
  your `gh` signed in, the install puts the GitHub keys in place itself. When it is not, the install asks you to sign
  in; if you choose to add the keys yourself, it shows each one with where it goes.
- **The log** keeps every detail: each command, its output and how long each step took. **Show the log** opens it.
  Each install and uninstall writes a new one in `/var/log/software-factory/`, readable by you and root.
- **When it stops**, it says in one sentence what failed and what to do, and where the log is. A first install that
  fails takes back what it made, unless you chose to keep it. Run it again after a fix: each step finishes what an
  earlier run left.
- **Uninstall** first shows what it would remove, changing nothing. Remove everything does it. The logs stay.

One action runs at a time. Close on the page stops the console once no action runs. Ctrl-C in the terminal stops it
at any time: it first lets a running action end, refusing any question the action still asks, and a second Ctrl-C
stops at once.

The same file runs in the terminal instead: `sudo python3 software-factory.pyz --terminal --repo <path>` prints each
phase with a check mark, asks its questions in the terminal (the QR code too), and with `--verbose` prints every
detail the log keeps. It takes `--healthchecks`, `--keep-on-failure` (which keeps the sealed copy too),
`--dry-run` and `--set key=value`, as the console does. `sudo python3 software-factory.pyz --uninstall [--repo
<path>] [--dry-run]` removes the factory.

From your own checkout of the factory, the same programs run a commit whose CI passed: `sudo python3
runtime/system/console.py --repo <path> [--commit <sha>]`, `sudo python3 runtime/system/install.py --commit <sha>
--repo <path>` and `sudo python3 runtime/system/uninstall.py`. They check the commit's CI on GitHub with your `gh`
first, and seal the commit from your checkout the same way.

## Start

1. Plug the laptop in and run `factory status`: the vitals, the sessions and both environments.
2. Start the orchestrator from the dashboard's Agents page, or with `factory start orchestrator`.

## Every day

- **Home** shows what needs you: up to three proposals, each with a picture and why now. Approve, reorder or say not
  now. Nothing starts until you approve it, except the fix for a clear prod outage.
- **Also needs you** holds the rest: a spec to sign off (open its brief, select text to comment on, press Go or Send
  feedback), a stuck task (Extend, To a designer or Park), and questions from sessions.
- **Boost** any queued task or request on the Work page to make it the next proposal.
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

Every product gets a database, traces (Langfuse) and errors (GlitchTip) from the factory: on the server for staging
and prod ([server.md](server.md)), and on the laptop for the stack slots ([laptop-services.md](laptop-services.md)).
Each new prod error arrives once as a request in the agents' inbox, for triage beside your feedback. Agents read
errors and traces with `factory errors` and `factory traces`. The factory's own failures report to a GlitchTip of its
own on the laptop, which no agent reaches ([laptop-services.md](laptop-services.md)).

## The product's tools

The agents and the stack run the product's scripts with your own tools: the install copies each program in
`install.toolchain` from your PATH, and the Python the product asks for (its `.python-version`, else its
`requires-python`) from your uv, into `/opt/software-factory-toolchain/`. The copies are root-owned and read-only, and
the factory's uv finds its Python there and never downloads one. When the product asks for a Python you have not
installed, the install stops and names it: run `uv python install <version>`, then install again. Sessions may also
write their tools' caches in the agent's home (`sandbox.writable_caches`), so the product's tests run inside their
sandbox. At every install, a drill runs each tool and finds the product's Python from inside a session.

The factory reads parts of Claude Code that no version promises to keep: its session records, the line `claude --bg`
prints, its transcripts. So the install copies only a Claude Code the drills passed with (`install.claude_lowest` up
to below `install.claude_untested`), the agents' copy never updates itself, and a drill starts, finds and stops a real
background session as the warden does. To move to a newer Claude Code, install again with
`--set install.claude_untested=<a higher version>` and check every drill passes. Should sessions ever run with no
record the warden reads, the vitals turn red and the crash watch resumes and retires nothing until they are seen.

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

The factory makes that account active, restarts the agents' Claude daemon, and brings back every agent that was
working or waiting, where it left off. You see each step as it runs. Until the daemon is back, no agent starts; until
the switch ends, another switch is refused. An agent that cannot come back says why in the last step, and the warden
then resumes it as it would after a crash.

Add an account with Add account on the same page, or with `factory account add <name>`, which asks for the token
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

1. Change the factory's repo by hand: branch, test, merge, push. The factory's CI publishes a release once every
   check passes.
2. Home shows **A newer factory is ready**, with what changed. **Upgrade** pauses the
   factory, waits for every agent to settle, and installs the release as root, after checking on GitHub that CI made
   it. While the dashboard is away, its address shows a page that says the factory is upgrading and comes back by
   itself. The upgrade also commits the product's `.github/factory-ref` and CI
   workflows for the new factory; that commit waits on Home for your Push to staging, like any other.
3. Run the server set-up again from a factory checkout at that commit (`python3 -m standard.server.setup --product
   <path>`). It refuses a checkout at another commit than `.github/factory-ref` names. Older builds stay deployable.
4. **Restart the factory** on Home: every agent comes back with its work.

The dashboard looks for releases every `install.release_check_hours`, with a read-only GitHub token for the
factory's repo, which the install asks for once, so the first upgrade to a factory that has this button is by hand,
from the release file. **Check now** on Settings, beside the factory's version, asks at once. An upgrade that needs
an answer (a new token, a key to add) stops and says so: run the release file by hand to give it.

## When something is wrong

- **Prod down:** a red band on Home. The factory starts one fix without you and ships it to staging; you push it to
  prod, or push back. Stop the fix from the band if you prefer to handle it yourself.
- **The warden unreachable:** the dashboard keeps its last known state, marked old, and refuses every change until
  the warden answers. Check its service on the laptop.
- **Vitals red:** nothing starts until memory, CPU or power recover, or the install hash matches an upgrade.
