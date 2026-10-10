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

The product's CI is the factory's template, copied in unchanged. It checks every push, and takes each push to the
default branch to staging through the product's own steps in `scripts/ci/`. The product also brings staging and prod
health addresses that answer JSON naming the running commit as `build_commit` ([repo-shape.md](repo-shape.md) has
the details).

When staging looks right, press Push to prod in the top bar and enter the code. The warden starts the product's
`deploy-prod.yml` with exactly the commit staging runs, then watches prod until it answers on it. If prod misbehaves,
push back the same way: the warden starts the same workflow with the commit prod ran before. It knows that commit
from its deploy log, which records each new build staging and prod answer with.

Your own checkout of the product runs the factory's git hooks too: a merge into the default branch there lands only
with a tree that passed `factory check`.

## Pause, stop, resume

- **Pause the factory** (code): every session, the orchestrator included, commits its work and pauses. Nothing is
  lost; Resume brings everything back in order.
- **Stop an agent** from the Agents page: it retires with everything under it, and its task returns to Proposed with
  its branch kept. Stopping the orchestrator stops the whole factory and needs the code.

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
4. In the product, move `.github/factory-ref` to that commit, and copy in `templates/ci/` again if it changed.
5. Start the orchestrator again.

## When something is wrong

- **Prod down:** a red band on Home. The factory starts one fix without you and ships it to staging; you push it to
  prod, or push back. Stop the fix from the band if you prefer to handle it yourself.
- **The warden unreachable:** the dashboard keeps its last known state, marked old, and refuses every change until
  the warden answers. Check its service on the laptop.
- **Vitals red:** nothing starts until memory, CPU or power recover, or the install hash matches a reinstall.
