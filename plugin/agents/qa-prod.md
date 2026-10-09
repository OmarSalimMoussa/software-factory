---
name: qa-prod
description: Use when the orchestrator starts you after a push to prod, or daily, to look at the live product read-only - a tester, in its own browser, that checks prod and staging pages load and behave, and never creates, edits or deletes anything.
# The warden renders model and effort from settings (models.qa_prod) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, Write, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 300
skills:
  - qa-prod
# The tester's own browser, started with it and stopped when it finishes.
mcpServers:
  - qa-browser:
      type: stdio
      command: factory
      args: ["qa-browser", "qa-prod"]
---

Follow the qa-prod skill.
