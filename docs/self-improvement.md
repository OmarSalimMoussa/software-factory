# How the factory improves itself

The factory runs for days with no person watching. Three things make that work. Agents report what goes wrong in
Self-reflect. Claude fixes the factory from those reports, outside the factory. And the warden heals what it can on
its own. Numbers on this page come from the factory's run on one product, through October 21, 2026.

## The outer loop

The inner loop is the agents' work on your product. The outer loop is Claude's work on the factory itself. It runs
in a Claude Code session that acts as the factory's operator, following the skill in
[.claude/skills/running-the-factory/](../.claude/skills/running-the-factory/SKILL.md).

1. **Watch.** The operator watches Self-reflect, the factory's own project in the error tracker, CPU use, the merges
   waiting and the plan's weekly use. It reads them through the [MCP server](mcp.md) and over SSH (a secure remote
   login), every few minutes.
2. **Triage.** Each new entry is one of three things. A fault in the factory gets fixed. A fault in the product goes
   to the orchestrator as a request. An entry that needs no change is marked ignored, with the reason.
3. **Fix.** Each fix goes to a subagent in its own git worktree. The subagent finds the root cause first, then writes
   a failing test, then the fix. Several fixes run at once when they touch different files.
4. **Verify.** The combined fixes must pass lint, types, every test, the browser tests of the dashboard, and the
   self-check. The self-check holds the factory to the same standard checks it holds every product to.
5. **Install.** The `factory.upgrade` action installs the new commit. Agents pause, then come back with their work.
   To go back, install the commit before.
6. **Mark.** Each entry the fix addressed is marked fixed, with the commit as its note.

Two rules keep the loop sound:

- **The factory never changes itself.** Agents never edit the factory, and the factory never works on its own repo.
  Every change to it comes from Claude or the owner, so the system being improved can't rewrite its own limits.
- **Fixes go into code, not prompts.** A fault becomes a guard rule, a warden check, or a tool that waits for what
  it needs. Or it becomes a refusal that names the exact command to run instead. The fix is replayed on the real
  transcripts that showed the fault before it is trusted.

## Self-reflect

Self-reflect is the one place where the factory writes about itself. Agents add to it with
`factory reflect --kind <kind> "<one or two sentences>"`. The warden adds its own entries.

| Kind | Written when | Written by | Entries |
| --- | --- | --- | --- |
| friction | A rule, limit or tool stopped good work for no good reason | Any agent | 240 |
| discovery | The factory behaves differently from what its docs say | Any agent | 88 |
| miss | A problem got past the agent, or past the role that should have caught it | Any agent | 55 |
| incident | Production went down, the orchestrator crashed or went silent, the factory paused itself, Docker's disk filled, or one rule kept refusing one agent | The warden | 39 |
| owner-feedback | The owner said how the factory should work, quoted word for word | The orchestrator | 9 |
| misroute | A task had to move to another rung | The orchestrator | 2 |

Of the 433 entries, 289 were fixed, 138 were ignored with a reason, and 6 are open.

The rules that keep it honest:

- **Short and specific.** An entry says what happened and what would have prevented it, in at most 600 characters.
- **Write-only for agents.** An agent learns nothing back, not even whether the same entry already exists. Only the
  dashboard, and Claude through the MCP server, can read it.
- **About the factory only.** A problem with the product is a request, not a reflection. A refusal that was right is
  not a complaint.
- **Repeats fold.** The same kind and text within 24 hours adds to the entry's count instead of adding an entry.
- **Fixes are checked.** An entry stays open until it is marked fixed or ignored. If its problem comes back, it
  reopens, marked as come back, with the note of the fix that did not hold.

The Self-reflect page leads with how many entries are open and how many new ones came each day. Its trend shows
whether the factory is getting healthier. Entries could first be marked fixed on October 17.

| Day | New entries | Closed | Open at the day's end |
| --- | ---: | ---: | ---: |
| Oct 12 | 13 | 0 | 13 |
| Oct 13 | 1 | 0 | 14 |
| Oct 14 | 33 | 0 | 47 |
| Oct 15 | 20 | 0 | 67 |
| Oct 16 | 22 | 0 | 89 |
| Oct 17 | 76 | 160 | 5 |
| Oct 18 | 106 | 99 | 12 |
| Oct 19 | 92 | 87 | 17 |
| Oct 20 | 32 | 33 | 16 |
| Oct 21 | 38 | 48 | 6 |

## Healing without a person

The warden repairs what it can on its own, and files an incident in Self-reflect when it does. Values below are the
defaults in [settings.toml](../settings.toml).

- **Crashed sessions resume.** When an agent's process ends, the warden resumes it, waiting twice as long each time.
  After 2 failed resumes within an hour, it retires the session, and its task goes back to Proposed with its branch
  kept. The orchestrator is always resumed.
- **A silent orchestrator restarts.** When the orchestrator's loop is silent for 40 minutes, or 2 hours while it is
  busy, the warden restarts it.
- **Outages are told apart from network trouble.** The warden probes production every minute. A failure counts only
  when production answered wrongly or timed out while a test request to GitHub got through. Two failures in a row
  make an outage, and the factory starts one fix without waiting for you.
- **Errors become requests.** Each new production or staging error in the error tracker arrives once, as a request.
  An error that comes back after its fix reached that environment is filed again.
- **Broken merges are found and reverted.** When a saved scenario fails after a batch of merges, testers walk it again
  between the suspect merges. The merge it fails with twice is reverted, and its task goes back to its implementer.
- **Work survives interruptions.** A paused or pushed-out session commits its work and later resumes with its full
  conversation. A pause, a restart or an upgrade loses no work.
- **Loops surface.** When one guard rule refuses one agent 5 times within an hour, the warden files an incident. A
  stuck agent shows up instead of burning time.
- **A heartbeat.** With the install's `--healthchecks` option, the warden and the orchestrator ping
  Healthchecks.io. If the whole server stops, you hear about it.

[cost-and-vitals.md](cost-and-vitals.md) covers the limits that stop new work: cost, plan use and the machine's
vitals.
