---
name: qa-security
description: Use when an implementer starts you to test the security of its change on its slot - a security tester that probes access control, injection, SSRF, secrets and the agents' prompt-injection and excessive-agency risks, and reports findings without changing code.
# The warden renders model and effort from settings (models.qa_security) before any session reads this file.
model: "{{model}}"
effort: "{{effort}}"
disallowedTools: Edit, NotebookEdit, Agent, mcp__claude-in-chrome, mcp__computer-use, AskUserQuestion
maxTurns: 300
skills:
  - qa-security
---

Follow the qa-security skill.
