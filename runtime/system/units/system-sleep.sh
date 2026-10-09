#!/bin/sh
# Before the laptop sleeps, pause the warden's and the orchestrator's alerts; their next check-in resumes them.
exec @python@ -I @install@/runtime/system/pause_checks.py "$1"
