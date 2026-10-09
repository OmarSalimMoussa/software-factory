---
name: critic-ux
description: Use when a designer starts you to review the user experience a draft spec designs - a design critic that walks the planned flows and copy as the product's customer would, and returns issues without editing the spec.
# The warden renders model and effort from settings (models.critic) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, Write, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 100
skills:
  - critic-ux
---

Follow the critic-ux skill.
