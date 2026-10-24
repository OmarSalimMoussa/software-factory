# Cost, time and vitals

A long-running agent system has to know what it spends, where its time goes, and whether its machine is healthy. The
warden measures each of them, and each can stop new work before it does harm. Values below are the defaults in
[settings.toml](../settings.toml). Numbers come from the factory's run on one product, through October 21, 2026.

## How cost is measured

- **Every model call is counted.** The warden reads each agent's Claude Code transcript, only the lines added since
  its last pass. Claude Code writes one call as two or three lines, so the warden counts each call once, by its
  message and request ids.
- **Every call is priced.** Prices are Anthropic's API list prices, per model, for input, output, cache reads and
  cache writes. A model with no price costs "unknown", never zero. On a Claude subscription, these dollars estimate
  the API cost; they are not the bill.
- **Every dollar has an owner.** A session's calls go to its task. A subagent's calls go to its parent's task. An
  intake run goes to the request it studies, then to the task that request joins. The orchestrator and triage are
  upkeep: they belong to no task.

Over the run, the agents made 50,968 model calls, at an estimated $1,789:

| Where the money went | Cost |
| --- | ---: |
| Tasks that merged (67) | $765 |
| Upkeep: work for no single task, such as the orchestrator, triage and regression checks | $665 |
| Tasks not merged: in progress, parked or closed | $359 |

| Model | Cost |
| --- | ---: |
| Opus | $1,497 |
| Sonnet | $264 |
| Haiku | $28 |

98% of the input tokens the agents read came from the prompt cache. On Opus and Sonnet, a cache read costs a
twentieth of fresh input.

## The cost of a merge

The Spend page shows the **average cost of a merge**: the mean cost of every merged task, from its first run to its
merge. It counts the task's designer, critics, implementer and testers, and the intake runs of its requests.

| Rung | Merges | Average cost |
| --- | ---: | ---: |
| 1, a small implementer | 30 | $3.36 |
| 2, a large implementer | 26 | $12.54 |
| 3, a designer, then an implementer | 10 | $27.86 |
| 4, a designer, your Go, then an implementer | 1 | $60.03 |
| All rungs | 67 | $11.42 (median $6.04) |

For the 59 merges whose cost is traced to every agent that worked on them ($670), the split by role is:

| Role | Cost | Share |
| --- | ---: | ---: |
| Implementers | $475 | 71% |
| Designers | $95 | 14% |
| Testers | $77 | 11% |
| Critics | $20 | 3% |
| Helpers | $2 | under 1% |

**Estimates.** Each proposal shows what it will likely cost: the median cost of the 20 most recent merged tasks at
its rung, once 5 are known. You see the price before you approve.

## What keeps cost down

- **The cheapest rung that can do the job.** The orchestrator plans each task on the lowest rung that can do it. A
  task that needs a higher rung comes back as a new proposal, and the misroute goes to Self-reflect.
- **A model for each role.** Opus does the work that needs judgment: the orchestrator, designers and large
  implementers. Sonnet runs small implementers, intake, triage, critics and most testers. Haiku runs the testers that
  follow written steps: interface, regression and production checks. You change any role's model in Settings.
- **A daily budget.** Past $150 a day, at API prices, only severe work starts that day.
- **The plan's limits.** The orchestrator's status line reads the Claude plan's 5-hour and 7-day use. A reading older
  than 45 minutes counts as unknown. At 80% the dashboard turns amber, and at 95% red.
- **An automatic pause.** When the plan's weekly use reaches `cost.plan_pause_percent`, the warden pauses the factory,
  says why, and waits for you. It never switches Claude accounts on its own.

## Where the time goes

The Time page measures each task that reached staging in the past 7 days.

- **Wall time and runtime are kept apart, never added.** Wall time is calendar time between a task's moves. Runtime is
  the time its agents were doing something.
- **Runtime splits four ways:** the model thinking, tools running, waiting on subagents, and idle.
- **The lead and the helpers are apart.** The designer and implementer lead. Testers and critics help.
- **A merge's wait for its check splits three ways:** testers walking, recovering after a failed check, and waiting.
  The page shows the median, the 75th percentile and the longest.

## Vitals

The warden reads the machine's vitals every 15 seconds. Each is green, amber, red or unknown. **Red stops every new
start. Amber lets only severe work start. Unknown counts as amber, never as fine.**

| Vital | Amber | Red |
| --- | --- | --- |
| Free memory | Under 8 GiB | Under 4 GiB |
| CPU use, the machine's average | Over the CPU target, 55% | Over 90% |
| Free disk | Under 30 GiB, or a clean-up problem | Under 10 GiB |
| Power | On battery, or under 30% charge | Under 15% charge |
| Install | | The installed copy differs from what the installer recorded |
| Git hooks | | The product's checkout does not run the factory's hooks |
| Claude daemon | | The agents' Claude Code daemon is not running |
| Firewall | | Loaded more than 3 minutes ago |

**The CPU budget.** Some hosts meter CPU on credits, and throttle the whole machine, production included, once they
run out. The warden caps the factory's own share of the CPU by what runs outside it, never under 15%. The machine's
average then settles at the target. Past 65%, new browser testers wait for a while.

**Merges waiting.** Merges waiting for their check turn amber when 5 wait, or the oldest has waited 6 hours. Unlike
the vitals, this never stops a start: it would hold back the very fixes that clear those merges.
