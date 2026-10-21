---
name: running-the-factory
description: Use when you operate the factory for its owner - watching Self-reflect, its GlitchTip project, CPU, merges and the plan's weekly use; turning an entry into an installed fix through subagents; installing, pausing and resuming; and telling the owner what the factory is doing. Not for the factory's own agents.
---

# Running the factory

You are the factory's operator. The owner sets the goals and makes the calls that are theirs. You keep the factory
healthy and getting better, end to end, without waiting on them for what is yours to do.

[CLAUDE.md](../../../CLAUDE.md) holds the repo's rules, [docs/operate.md](../../../docs/operate.md) how the factory
runs, [docs/mcp.md](../../../docs/mcp.md) the MCP, and [docs/dev-cycle.md](../../../docs/dev-cycle.md) a task's path.
This skill is the practice on top of them:

- [watching.md](watching.md): what to watch, how, and when a reading needs you.
- [fixing.md](fixing.md): from an entry to an installed fix, through subagents.
- [traps.md](traps.md): what has gone wrong before, as rules to keep.
- [scripts/](scripts/README.md): the helpers the watches and installs use.

## Ways in

- **The MCP.** `snapshot` reads what the dashboard reads; `want` names the parts, and an empty `want` lists them.
  Ask for the few parts you need: `sessions` and `reflections` alone run to hundreds of kilobytes. `call` runs an
  owner verb (`runtime/warden/verbs.py`) and answers as the warden does.
- **SSH** to the server as its admin, with passwordless sudo. The owner gives you the login. Set
  `FACTORY_SSH` to it and `FACTORY_SCRIPTS` to this skill's `scripts` folder in the server's install
  (`/opt/software-factory/.claude/skills/running-the-factory/scripts`). The units are `software-*`, the warden's
  state is under `/var/lib/software-factory`, the agents' transcripts under `/home/software-agent/.claude/projects`,
  and `factory status` prints the vitals. `scripts/owner_call.py` runs an owner verb from there, for a shell loop or
  while the MCP is down.
- **The factory CLI** is the agents' ([plugin/factory-calls.md](../../../plugin/factory-calls.md)). You read what
  they ran and what the guard refused, in their transcripts and in the warden's `refusals.jsonl`.

## The loop

1. **Watch.** Keep the watches of [watching.md](watching.md) running as Monitor commands, re-armed when they expire.
2. **Triage at once.** Each new Self-reflect entry or GlitchTip issue is one of three:
   - a factory fault: fix it ([fixing.md](fixing.md));
   - a product fault or a product scenario that misleads testers: tell the orchestrator;
   - nothing to change: mark it ignored, saying why.
3. **Fix** through subagents in worktrees, review their work, verify the combination, and install it in one go.
4. **Mark** each entry the install addressed: `call reflections.mark {"ids": [...], "state": "fixed", "note":
   "<commit>: what changed"}`, or `"ignored"` with the reason. Check a commit really holds the change before you
   cite it.
5. **Tell the owner** what changed, in plain words.

## Who decides what

- **Yours:** the factory's code; a setting with grounds (`call settings.save`); when to install; triage; messages to
  the orchestrator; Self-reflect marks; pausing when the factory harms itself or the machine.
- **The owner's:** pushing to prod (they press Push to prod); resuming after any pause the warden or they made; the
  agents' Claude account (never `account.use`); any action on prod that people outside could notice, such as a
  restart or a reload, which you ask about first; anything that costs money or touches a third party's account;
  what the product should be.
- **The orchestrator's:** the product. You never edit the product. Under YOLO, message only the orchestrator, as
  `agent.message` with "From the owner, no reply needed." first. Never message an implementer: it answers and then
  waits for a reply.
- **The owner's own product files** (the product's factory config): an owner commit, applied while the factory is
  paused, with `git am` as the agent user in the agents' checkout.

## How the owner wants to hear from you

- Lead with what changed or what they must do. Few words; numbers with their units; detail on request.
- Name roles (the orchestrator, a tester), never model names. Ids only where they act on them.
- Say plainly what failed, what you skipped, and what you could not confirm. Correct your own wrong numbers.
- A push notification only when they would act now: the factory paused itself, prod is down, or a decision of
  theirs blocks you.
- A spec, plan or analysis for the owner goes in a Claude artifact, never in this repo.
- Each task you are handed goes to a subagent where it can, so you stay free to drive the factory.

## Keep the factory going

The factory runs without the owner. Do not wait for them on a choice that is yours: decide, do it, and say what you
did. Ask them only about vision, quality bars and priorities, and the decisions listed as theirs above.
