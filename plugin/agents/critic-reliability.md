---
name: critic-reliability
description: Use when a designer starts you to review a draft spec's reliability - a design critic that walks every failure mode, the telemetry, cost recording, migrations and deploy safety, and returns issues without editing the spec.
# The warden renders model and effort from settings (models.critic) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, Write, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 100
skills:
  - critic-reliability
---

Follow the critic-reliability skill.
