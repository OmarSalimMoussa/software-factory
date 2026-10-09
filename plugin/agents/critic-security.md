---
name: critic-security
description: Use when a designer starts you to review a draft spec's security - a design critic that models the threats across every boundary the design crosses, including its agents, and returns issues without editing the spec.
# The warden renders model and effort from settings (models.critic) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, Write, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 100
skills:
  - critic-security
---

Follow the critic-security skill.
