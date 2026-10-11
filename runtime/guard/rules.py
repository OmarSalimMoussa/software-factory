"""Every guard rule the owner can switch off, with what it refuses: the warden checks a switch against these names,
and the dashboard lists them. A failure of the guard itself is no rule and cannot be switched off."""

from __future__ import annotations

RULES: dict[str, str] = {
    "unpermitted": "anything but reading and reporting, in a session the warden never started",
    "retired": "every call from a retired session",
    "stopped-subagent": "every call from a subagent the owner stopped",
    "bench-pending": "anything but staging, committing and the factory command, in a session being paused",
    "raw-starts": "running `claude`, except to list agents, read logs or print its version",
    "leave-slice": "sudo, systemd-run, at, crontab, su, systemctl --user, docker, podman, ssh, xdg-open, gio and kin",
    "factory-verbs": "a `factory` verb the role, or a subagent of it, may not use",
    "factory-alone": "a call running `factory` that is not one plain `factory …` command, or names outside files",
    "queue-status": "a task status set by anyone but the orchestrator; any write to the queue but through `factory`",
    "subagent-kinds": "a subagent kind not on the role's list; any start from inside a subagent",
    "subagents-at-once": "more subagents than the cap; for the orchestrator, one place is kept for intake",
    "model": "an Agent call asking for any model but that kind's configured one",
    "tester-brief": "a tester brief without one `Scenarios and checks: N` line",
    "tester-time": "a tester's calls after its time; command timeouts are cut to the time left",
    "intake-time": "intake's calls after intake.fast_lane_minutes, but for `factory` and its hand-back",
    "qa-ready": "a tester start with no ready record for the head",
    "qa-rounds": "a QA round past its setting",
    "critic-rounds": "a critic round past its setting",
    "resume-subagent": "a message to a finished or stopped subagent",
    "git-bypass": "skipped hooks, rewritten refs, config or remote changes, any push, a merge without --no-ff, "
    "a hand-run check, GitHub writes",
    "own-branch": "a git command that moves a ref other than the session's own branch, or adds or removes a worktree",
    "prod-frozen": "running the deploy scripts by hand, starting a workflow, pushing a tag or another branch",
    "protected-paths": "writes to the factory's own files and the product's protected paths",
    "own-worktree": "Edit, Write, MultiEdit and NotebookEdit outside the session's own worktree",
    "subagent-writes": "a subagent's write in a checkout outside its session's .factory/, or a git command that "
    "changes anything",
    "orchestrator-writes": "the orchestrator's write in its checkout outside .factory/, or a commit on the default "
    "branch that finishes no merge or revert",
    "product-drivers": "a browser or computer-use tool outside a UI tester; the owner's Chrome, always",
    "qa-browser": "code run inside the browser server; a browser call that names a file",
    "qa-evidence": "a path or address into the factory's state, the testers' evidence or sign-ins, or the dashboard",
    "banned-tools": "workflows and remote triggers",
    "schedules": "any schedule kept by an agent: the warden drives the loop",
    "unreadable-command": "a command the guard cannot read, or one naming what runs through a variable, alias or pipe",
    "agent-id": "an agent-written --agent-id: only the guard names a subagent on its factory calls",
    "inbox-wait": "the orchestrator going idle without `factory inbox --wait` in the background",
}
