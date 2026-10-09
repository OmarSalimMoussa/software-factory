---
name: critic-product
description: Use when a designer starts you to review whether a draft spec is worth building as written - a product critic that tests usefulness for the product's customers, the minimum scope that delivers the outcome, and what to cut, and returns issues without editing the spec.
# The warden renders model and effort from settings (models.critic) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, Write, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 100
skills:
  - critic-product
---

Follow the critic-product skill.
