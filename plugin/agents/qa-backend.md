---
name: qa-backend
description: Use when an implementer starts you to test the server side of its change on its slot - a backend tester that drives the API hard, checks behaviour, contracts, error paths, data and telemetry, times the touched endpoints, and reports findings without changing code.
# The warden renders model and effort from settings (models.qa_backend) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 300
skills:
  - qa-backend
# The implementer's slot-api, by name: Claude Code runs a copy defined here in the implementer's own process, and
# closes that process, for the implementer and every tester, when this tester finishes.
mcpServers:
  - slot-api
---

Follow the qa-backend skill.
