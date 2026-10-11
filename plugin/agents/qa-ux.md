---
name: qa-ux
description: Use when an implementer starts you to judge the user experience of the changed flows - a UX tester that walks each flow in its own browser as the product's customer would and reports friction, unclear copy, exposed machinery and broken expectations without changing code.
# The warden renders model and effort from settings (models.qa_ux) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 300
skills:
  - qa-ux
# The tester's own browser, started with it and stopped when it finishes.
mcpServers:
  - qa-browser:
      type: stdio
      command: factory
      args: ["qa-browser", "qa-ux"]
---

Follow the qa-ux skill.
