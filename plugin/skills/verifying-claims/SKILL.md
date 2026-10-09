---
name: verifying-claims
description: Use before saying work is done, fixed, passing, clean, merged or on staging - run the check that proves the claim in this turn and quote its result.
---

# Verifying claims

No claim without fresh evidence from this turn. "Should work" is not a result.

| Claim | Evidence that proves it |
|---|---|
| tests pass | the test run's last lines, for the tests you picked |
| a bug is fixed | the reproduction now passes on your slot, and the new test passes |
| QA is clean | `factory qa-clean` accepted, and the last round's verdicts |
| the checks pass | the end of `factory check`, with the tree it recorded |
| merged | the merge commit on the default branch |
| on staging | `factory status` shows staging healthy on that commit |

- Run the check, read its output, then write the claim with the output beside it.
- Say what you did not check. Partial is partial: name the part that is left.
- A check you could not run is a blocker to report, not a step to skip.
- Never weaken a test, a check or a scenario to make a claim true.
