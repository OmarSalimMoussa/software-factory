---
name: debugging
description: Use when a test fails, a bug is reported, or the product behaves wrongly, before changing any code - confirm the bug where it was reported, find the root cause, then fix it with a test that fails first.
---

# Debugging

A fix without a cause is a guess. Find the cause first, then fix it once.

1. **Confirm the bug where it was reported.** A report about staging or prod is checked against its error-tracker
   issue first, when your session has the tracker's tools. Do not inherit the claim from a doc, a task or another
   agent.
2. **Reproduce it on your own slot.** A tester walks it through the product's screens or its API; an implementer
   never drives the product itself. Write down the exact steps and what the tester saw. No reproduction, no fix:
   tell the orchestrator what was tried.
3. **Find the root cause.**
   - Read the whole error and its trace, and the logs around it for the service that raised it.
   - Trace the wrong value back to where it is made, not where it shows.
   - Compare with a path that works, and list every difference.
   - Test one idea at a time with the smallest change or probe. Never stack fixes.
4. **Write the failing test first,** at the seam a caller sees. See it fail for the reason you found, fix the cause,
   see it pass.
5. **Keep telemetry honest.** A platform failure must report, and a new failure path gets its test. A user's
   failure goes to the UI, never to the error tracker.
6. **Three fixes that did not hold mean your model is wrong.** Stop, write down what you know and what you ruled out,
   and ask the orchestrator. After `routing.cause_tries` tries without a confirmed cause, report `needs rung <N>`.

Report the cause, the fix, the test, and how you saw it work on your slot.
