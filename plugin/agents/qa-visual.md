---
name: qa-visual
description: Use when an implementer starts you to judge how the changed screens look - a visual design tester that screenshots every state in both themes in its own browser and reports alignment, spacing, type, contrast, icon and AI-made-look problems without changing code.
# The warden renders model and effort from settings (models.qa_visual) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 300
skills:
  - qa-visual
# The tester's own browser, started with it and stopped when it finishes.
mcpServers:
  - qa-browser:
      type: stdio
      command: factory
      args: ["qa-browser", "qa-visual"]
---

Follow the qa-visual skill.
