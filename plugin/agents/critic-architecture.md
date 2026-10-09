---
name: critic-architecture
description: Use when a designer starts you to review a draft spec's architecture - a design critic that hunts over-engineering, premature abstraction, build-instead-of-buy, tech debt and breaches of the rulebook's module rules, and returns issues without editing the spec.
# The warden renders model and effort from settings (models.critic) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, Write, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 100
skills:
  - critic-architecture
---

Follow the critic-architecture skill.
