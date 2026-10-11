---
name: intake
description: Use when the orchestrator starts you with one request (a severe one, in the fast lane) or one new task (a light pass after triage) - to place it in the code and the telemetry, write the task's routing facts, and hand back a five-line verdict so the orchestrator never reads code.
# The warden renders model and effort from settings (models.intake) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 100
skills:
  - intake
---

Follow the intake skill.
