# The dev cycle

Every change moves from a request to staging. Nothing reaches prod until the owner pushes it, and nothing starts
without the owner's approval, except the fix for a clear prod outage. Each role's skill in
`plugin/skills/` says how it does its part; the guard and the warden enforce the limits in code.

## The steps

1. **Requests arrive** in one of three inboxes: from factory agents and the warden, from the owner (filed by the
   orchestrator, word for word), and from users (the optional report pull).
2. **Intake and triage.** A severe request goes to an intake agent at once and becomes a top task within minutes.
   The rest wait for a batch, which a triage agent turns into a few tasks: each request becomes a new task, joins
   one, is noise, or is parked. `factory triage apply` checks the plan and writes all of it or none. When the owner
   changes the goal items in `GOAL.md`, every parked request and task comes back. The next triage, due at once,
   judges each task against the new items: it is kept for the item it serves now, or parked again. Until then it
   cannot be proposed.
3. **Proposals.** The orchestrator picks at most `routing.max_proposals` tasks that take the owner closest to the
   goal, plans each on the cheapest rung that can do it, and proposes it with a small picture and why now. A task
   the owner boosted, or a severe one, goes first, even when every place is held.
4. **Approval.** The owner approves, reorders or says not now on the dashboard. Only an approved run starts.
5. **Design, at rung 3 or 4.** A designer writes the spec, runs critics until a round raises nothing new, and
   saves a brief, one page kept on this machine with the task. The orchestrator signs off rung 3. Rung 4 goes to the
   owner, who reads the brief on the dashboard, selects text to comment on, and presses Go.
6. **Build.** An implementer works in its own worktree and slot, reproduces a bug before fixing it, and builds every
   slice tests first. A bug only a browser shows is reproduced by testers after `factory qa-ready <head> --reproduce`,
   before the branch's first commit beyond Markdown, such as its spec: no QA round, and none of their results counts
   toward `qa-clean`.
7. **QA.** `factory qa-ready` records the head; every tester the change's surfaces require runs at once. A round is
   a head. Rounds end when one finds no new blocker or major, at most `rounds.qa`. `factory qa-clean` is refused
   until every required tester passed on the final head.
8. **Merge turn.** One branch at a time: the warden gives the turn to the next ready task and tells its
   implementer, which rebases and has the warden run `factory check`, at most `rounds.check_runs` times. The
   orchestrator merges with `--no-ff`; the merge hook lets only the turn's branch land, with the pass for its tree.
   The warden finds the merge itself.
9. **Check the default branch, in bundles.** A merge waits for its check from when the warden finds it. A bundle is
   the oldest waiting merges, at most `rounds.bundle_merges`, walked together at one commit: the one just before the
   next merge, or the tip. Slot 0 comes up there, whole, from a worktree of its own when that commit is older than
   the tip, and regression testers walk, in parallel, every scenario whose `protects` names a path one of them
   changed. Their runs go into the evidence with the saved check, so its page in Work shows each tester's screenshots
   and the replay of its walk. The clean-up keeps the QA of the newest `qa.keep_bundles` checks that cleared a merge;
   past them, the screenshots and replays of a check, or of a task it cleared, go, and their pages say when.
   Transcripts are never removed. A check clears every merge waiting up to its commit, at once, only when every row
   passed, each of those scenarios has a row, slot 0 ran that commit, and they are within the cap; anything less
   clears none. No merge turn goes while the cap of merges waits, until their bundle's check failed. Each row that
   did not pass says what comes next: walk it again once what blocked it is fixed (a block is never a revert); walk
   it alone at the bundle's base (or, where slot 0 cannot come up there, at the next merge's place), or between its
   suspects (the merges since its last pass whose changed paths it protects); revert the one suspect left once
   `rounds.revert_fails` walks with it in failed (one fail of a scenario that sometimes fails is no proof), which
   sends its task back to its implementer; or, when it fails at the oldest of those too, wait for its fix, which
   takes the next turn and joins the bundle past the cap. When only the owner can give what a merge needs (a key, an
   account, leave to use an outside site), it is reverted and its task blocked: it waits on Home for the owner's
   **Fixed, carry on**, its implementer benched and written to SELF_REFLECT, while other work goes on. When another
   open task makes it, the task is on hold, and the warden sends it back once that task is checked, or closed or
   parked; the orchestrator may release a hold whose task cannot pass soon, and it then no longer waits for it. A fix
   the merge carried for what every task shares, such as a build or ignore file, is applied again right after the
   revert, in one commit of its own; one that no longer applies cleanly becomes a request.
10. **Staging** is slot 0: after each check it comes back up at the default branch's tip, so it runs the newest
    merge. The warden mirrors the default branch to GitHub; no agent pushes, asks for a push or waits on one.
11. **Prod.** Only the owner pushes to prod, from the dashboard, at exactly the commit staging runs, once every merge
    since prod's build passed its check. The factory builds that commit on the server and deploys it. Pushing back
    redeploys the commit prod ran before.

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

Working and waiting sessions hold a cap slot. Only severe work, or work for a higher goal item, can push a running
session out, and a working one only after `watch.bench_min_run_minutes` unless the new work is severe. The session
is told to pause, commits its work, and comes back later with its full context. Only finishing, the owner's
Stop, or a crash retires a session, and a retired session never comes back: its task returns to Proposed, and a new
session starts from its branch. An implementer is benched as its merge lands, and holds no place while the default
branch is checked: it comes back only if its task is sent back to it. The warden sees a session finish from its
task's status (an implementer's task checked, a designer's spec gone to build): it stops the session, empties its
slot and frees its place, on the bench or off it.

A session whose process ends is resumed, each time after a longer wait, and retired as crashed only after
`watch.resume_tries` resumes within `watch.crash_window_minutes`. Nothing resumes until the agent's Claude daemon is
up, and sessions that ended with it (a reboot, a daemon restart) come back without counting as crashed.
