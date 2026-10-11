# Operating the factory

What the owner does, day to day. The dashboard is the one window; the code is the 6-digit code from the
authenticator app set up at install.

## Start

1. Plug the laptop in and run `factory status`: the vitals, the sessions and both environments.
2. Start the orchestrator from the dashboard's Agents page, or with `factory start orchestrator`. It needs no code.

## Every day

- **Home** shows what needs you: up to three proposals, each with a picture and why now. Approve, reorder or say not
  now. Nothing starts until you approve it, except the fix for a clear prod outage.
- **Also needs you** holds the rest: a spec to sign off (open the artifact, comment on it, press Go or Check
  feedback), a stuck task (Extend, To a designer or Park), and questions from sessions.
- **Boost** any queued task or request on the Work page to make it the next proposal.
- **Talk to the orchestrator** in its chat. What you ask becomes a request in your words. What you say about the
  factory itself goes to `SELF_REFLECT.md`.

## Ship

The product's CI is the factory's templates, copied in unchanged. It checks every push, and builds each push to the
default branch, smoke-tests it and deploys it to staging, the standard way the product's `factory.toml` describes
([ci.md](ci.md)). The server's deploy command does the rest ([deploy.md](deploy.md)). Staging and prod each answer a
health address with JSON naming the workspace, the running commit as `build_commit`, and the work still running as
`busy`.

When staging looks right, press Push to prod in the top bar and enter the code. The warden starts the product's
`deploy-prod.yml` with exactly the commit staging runs, follows its deploy job, then watches prod until it answers on
it. The release steps run last (a browser extension's store upload, when it changed); a failed release shows as the
push's last step, with prod already live. If prod misbehaves, push back the same way: the warden starts the same
workflow with the commit prod ran before. It knows that commit from its deploy log, which records each new build
staging and prod answer with.

Your own checkout of the product runs the factory's git hooks too: a merge into the default branch there lands only
with a tree that passed `factory check`.

## Errors and traces

Every product gets a database, traces (Langfuse) and errors (GlitchTip) from the factory: on the server for staging
and prod ([server.md](server.md)), and on the laptop for the stack slots ([laptop-services.md](laptop-services.md)).
Each new prod error arrives once as a request in the agents' inbox, for triage beside your feedback. Agents read
errors and traces with `factory errors` and `factory traces`; the factory's own failures report to the laptop's
GlitchTip too.

## The product's tools

The agents and the stack run the product's scripts with your own tools: the install copies each program in
`install.toolchain` from your PATH, and the Python the product asks for (its `.python-version`, else its
`requires-python`) from your uv, into `/opt/software-factory-toolchain/`. The copies are root-owned and read-only, and
the factory's uv finds its Python there and never downloads one. When the product asks for a Python you have not
installed, the install stops and names it: run `uv python install <version>`, then install again. Sessions may also
write their tools' caches in the agent's home (`sandbox.writable_caches`), so the product's tests run inside their
sandbox. At every install, a drill runs each tool and finds the product's Python from inside a session.

## Pause, stop, resume

- **Pause the factory** (code): every session, the orchestrator included, commits its work and pauses. Nothing is
  lost; Resume brings everything back in order.
- **Stop an agent** from the Agents page: it retires with everything under it, and its task returns to Proposed with
  its branch kept. Stopping the orchestrator stops the whole factory and needs the code.

## Switch the Claude account

The agents sign in to Claude with one of your accounts at a time. To move them to another, when one nears its
limits:

- **On the dashboard:** open Settings. Under Claude account, press Switch on the account you want. No code is needed.
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

Change any setting on the Settings page and save with the code. Each value is checked before it is saved, the save
is committed to the factory's repo, and only what reads that value reloads. See [settings.md](settings.md).

## Improve the factory

The factory never changes itself. Read the Self-reflect page: misses, friction, discoveries, misroutes and incidents.
To act on one:

1. Pause the factory, then stop every agent.
2. Change the factory's repo by hand: branch, test, merge, push.
3. Reinstall from the reviewed commit: `sudo python3 runtime/system/install.py --commit <sha> --repo <path>`. The
   installer refuses while any factory session is live.
4. Run the server set-up again from that commit (`python3 -m standard.server.setup --product <path>`): a deploy
   refuses a build planned by another factory commit than the server runs.
5. In the product, move `.github/factory-ref` to that commit, and copy in `templates/ci/` again if it changed.
6. Start the orchestrator again.

## When something is wrong

- **Prod down:** a red band on Home. The factory starts one fix without you and ships it to staging; you push it to
  prod, or push back. Stop the fix from the band if you prefer to handle it yourself.
- **The warden unreachable:** the dashboard keeps its last known state, marked old, and refuses every change until
  the warden answers. Check its service on the laptop.
- **Vitals red:** nothing starts until memory, CPU or power recover, or the install hash matches a reinstall.
