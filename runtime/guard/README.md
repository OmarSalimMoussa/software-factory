# Guard

The Claude Code hook on every tool call of every factory session. It works out who is calling, then checks the call
against each rule: shell commands, files, git, prod, the browser, subagents and the factory command itself. A refusal
says what to do instead.
