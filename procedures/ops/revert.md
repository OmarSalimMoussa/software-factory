# Reverting a merge

How the orchestrator takes a merge back off the default branch: after a failed check, or when staging regresses
after a push.

1. `git revert -m 1 <merge sha> --no-edit` on the default branch.
2. **A merge that added a migration** keeps its migration files and the models they create: revert with
   `--no-commit`, restore those paths from `HEAD`, then commit. A migration that drops or rewrites data is fixed
   forward instead, never reverted.
3. `factory check` on the default branch. A revert back to a state that already passed (the last merge taken off)
   is known at once and runs nothing; any other revert is checked from the newest commit that passed.
4. Check the revert like a merge: rebuild slot 0 with what it touched, and walk the scenario that failed, so it has
   a fresh pass before the work comes back.
5. Push it with `factory push`.
6. Send the task back to `building` with the reason, and tell its implementer. The fix starts from the reverted
   work: `git merge --ff-only <default branch>`, `git revert <the revert's sha>`, then the fix. A rebase alone would
   leave the work reverted.
