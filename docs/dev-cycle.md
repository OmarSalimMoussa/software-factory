# The dev cycle

Every change moves from a request to staging. Prod moves only when the owner pushes it. Nothing starts without the
owner's approval, except the fix for a clear prod outage. Each role's skill in `plugin/skills/` says how it does its
part; the guard and the warden enforce the limits in code.

## The steps

1. **Requests arrive** in one of three inboxes: from factory agents and the warden, from the owner (filed by the
   orchestrator, word for word), and from users (the optional report pull).
2. **Intake and triage.** A severe request goes to an intake agent at once and becomes a top task within minutes.
   The rest wait for a batch, which a triage agent turns into a few tasks: each request becomes a new task, joins
   one, is noise, or is parked. `factory triage apply` checks the plan and writes all of it or none.
3. **Proposals.** The orchestrator picks at most `routing.max_proposals` tasks that take the owner closest to the
   goal, plans each on the cheapest rung that can do it, and proposes it with a small picture and why now.
4. **Approval.** The owner approves, reorders or says not now on the dashboard. Only an approved run starts.
5. **Design, at rung 3 or 4.** A designer writes the spec, runs critics until a round raises nothing new, and
   publishes a brief. The orchestrator signs off rung 3. Rung 4 goes to the owner as a Claude artifact with mock-ups;
   the owner comments on it and presses Go.
6. **Build.** An implementer works in its own worktree and slot, reproduces a bug before fixing it, and builds every
   slice tests first.
7. **QA.** `factory qa-ready` records the head; every tester the change's surfaces require runs at once. A round is
   a head. Rounds end when one finds no new blocker or major, at most `rounds.qa`. `factory qa-clean` is refused
   until every required tester passed on the final head.
8. **Merge turn.** One branch at a time: the implementer rebases and has the warden run `factory check`, at most
   `rounds.check_runs` times. The orchestrator merges with `--no-ff`; the merge hook finds the pass for that tree.
9. **Check the default branch.** Slot 0 rebuilds what the merge touched, and regression testers walk the scenarios
   it could break, in parallel. A failure reverts the merge only when that merge is the one change to the
   scenario's code since its last pass; any other failure becomes a request.
10. **Staging.** `factory push` pushes the checked commit, and CI deploys staging. A regression there is reverted at
    once.
11. **Prod.** Only the owner pushes to prod, from the dashboard with the 6-digit code, at exactly the commit staging
    runs. Pushing back redeploys the commit prod ran before.

## The rungs

| Rung | Runs | When |
| --- | --- | --- |
| 0 | nothing | No code needs to change; the task closes with a reason |
| 1 | a small implementer | The cause is known to the file and line, few slices, no `access` surface or owner class |
| 2 | a large implementer | The cause is known or the ask is clear and contained, a few more slices |
| 3 | a designer, then an implementer | Anything bigger or less clear; the orchestrator signs off the spec |
| 4 | a designer, the owner's Go, then an implementer | A large change people will see, or a large or costly backend change |

A run that turns out to need a higher rung comes back to the owner as a proposal, with its branch kept, and is
written to `SELF_REFLECT.md` as a misroute.

## Which testers

| Surface | Required testers |
| --- | --- |
| `ui` | qa-ui-function, qa-ux, qa-visual |
| `api` | qa-backend, from rung 2; a rung 1 backend fix runs none |
| `access` | qa-security, at any size |
| `desktop` | qa-ui-function, on the slot's remote desktop |
| `none` | none; `factory check` only |

A task's surfaces are the union of what intake proposed and what the branch's diff touches, by the path prefixes in
`check-values/surfaces.toml`. An implementer may add testers, never drop one.

## Sessions

Working and waiting sessions hold a cap slot. An approved task that outranks a running one can push it out: the
session is told to pause, commits its work, and comes back later with its full context. Only finishing, the owner's
Stop, or a crash retires a session, and a retired session never comes back: its task returns to Proposed, and a new
session starts from its branch.
