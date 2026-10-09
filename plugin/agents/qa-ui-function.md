---
name: qa-ui-function
description: Use when an implementer starts you to test that the changed screens work - a UI tester that clicks through every control and state in its own browser on the implementer's slot, reads the console and the network, and reports findings with screenshots without changing code.
# The warden renders model and effort from settings (models.qa_ui_function) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, Write, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 300
skills:
  - qa-ui-function
# The tester's own browser, started with it and stopped when it finishes.
mcpServers:
  - qa-browser:
      type: stdio
      command: factory
      args: ["qa-browser", "qa-ui-function"]
---

Follow the qa-ui-function skill.
