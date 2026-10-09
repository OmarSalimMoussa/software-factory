---
name: taking-review
description: Use when testers, critics, the checks or the orchestrator send findings - check each one against the code and the spec before acting; fix what is right, file what is outside the task, push back with evidence on what is wrong.
---

# Taking review

Findings are claims to check, not orders. Read them all before changing anything.

For each finding:

1. **Check it.** Reproduce it on your slot, or read the code or the spec section it points at.
2. **Decide, and act:**
   - **Right, and inside the task:** fix it. A behaviour change gets its failing test first.
   - **Right, but outside the task:** file it as a request (`factory request new`), with the tester or critic as
     its source. Do not grow the task.
   - **Wrong:** say why, with evidence: a command and its output, a line of code, a section of the spec. The next
     round's tester sees your answer.
   - **Unclear:** ask the tester or critic one precise question.
3. **Record it** in the round's summary: the finding, your decision, and the evidence.

- No agreement for show: thank nobody, explain nothing you did not check.
- If a tester repeats a finding you disputed, stop arguing and ask the orchestrator to rule.
- Never weaken a test, a check or a scenario to make a finding go away.
