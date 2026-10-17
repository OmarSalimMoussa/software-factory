"""The kinds of long owner action the warden runs as an operation: the warden starts each, and the dashboard titles
it, says what it did and reloads what it changed. A push's kind also names it in the push log and the inbox."""

from __future__ import annotations

SETTINGS = "settings"
REPO = "repo"
PUSH = "push"
PUSH_BACK = "push back"
PAUSE = "pause"
RESUME = "resume"
ACCOUNT = "account"
MESSAGE = "message"
WORD = "word"
UPGRADE = "upgrade"

KINDS = (SETTINGS, REPO, PUSH, PUSH_BACK, PAUSE, RESUME, ACCOUNT, MESSAGE, WORD, UPGRADE)
