---
name: qa-regression
description: Use when the orchestrator starts you to walk standing QA scenarios on the default branch after merges - a regression tester, in its own browser on slot 0, that reports pass, fail or blocked for each scenario with evidence, and never changes code.
# The warden renders model and effort from settings (models.qa_regression) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, Write, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 300
skills:
  - qa-regression
# The tester's own browser, started with it and stopped when it finishes.
mcpServers:
  - qa-browser:
      type: stdio
      command: factory
      args: ["qa-browser", "qa-regression"]
---

Follow the qa-regression skill.
