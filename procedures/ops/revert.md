# Reverting a merge

How the orchestrator takes a merge back off the default branch after a failed check.

1. `git revert -m 1 --no-commit <merge sha>` on the default branch. Read `git diff --stat <merge sha>^1 <merge sha>`.
2. **Keep what must stay:** a fix the merge carried for what every task shares (a build or ignore file, a shared
   script), and a migration it added with the models that migration creates. `git checkout HEAD -- <those paths>`
   puts them back. A migration that drops or rewrites data is fixed forward instead, never reverted.
3. `git commit -m "Revert <merge sha>: <why>; keeps <what step 2 kept>"`: one commit. The guard lets the
   orchestrator commit on the default branch only while git holds the revert, so nothing is applied after it.
4. `factory check` on the default branch. A revert back to a state that already passed (the last merge taken off)
   is known at once and runs nothing; any other revert is checked from the newest commit that passed.
5. Check the revert like a merge: bring slot 0 up at it (`factory slot up`), and walk the scenario that failed, so it
   has a fresh pass before the work comes back.
6. Nothing to ask: slot 0, which is staging, now runs the revert, and it reaches prod with the owner's next Push to
   prod.
7. Send the task back: `factory queue task status <task> building --note "<why>; reverted as <the revert's sha>"`.
   The warden hands the note to its implementer. Once the task was checked, that implementer has ended: the warden
   puts the task back in the approved runs instead (a designed build back in the designed builds), and the new
   implementer's first message carries the note. Either one brings the work back first:
   `git merge --ff-only <default branch>`, `git revert <the revert's sha>`, then the fix.
