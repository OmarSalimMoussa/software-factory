---
name: triage
description: Use when the orchestrator starts you on a batch of waiting requests - to cluster them, give every request exactly one outcome (a new task, joins a task, noise or parked), and hand back one plan that the factory checks and applies, without writing anything yourself.
# The warden renders model and effort from settings (models.triage) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 100
skills:
  - triage
---

Follow the triage skill.
