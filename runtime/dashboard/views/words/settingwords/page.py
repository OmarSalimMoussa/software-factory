"""The settings page's words: each group the page draws, in its order, with its hint on when a change applies;
and each setting's label by its key."""

from __future__ import annotations

from runtime.dashboard.views.words.settingwords import machine, watch, work

GROUPS = [*work.GROUPS, *watch.GROUPS, *machine.GROUPS]
LABELS = {key: label for _, _, _, rows in GROUPS for key, label in rows}
