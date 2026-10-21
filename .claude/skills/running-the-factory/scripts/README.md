# The operator's scripts

Small helpers for the factory's operator, used by [the running-the-factory skill](../SKILL.md). They ask the warden
through the owner's door, as the dashboard does, or read the machine. Only `install.py` and an owner verb you send
with `owner_call.py` change anything.

**On the server**, each runs from the installed copy of this folder, and finds the factory's code beside it. Run them
as the server's admin; the CPU sample and the limit watch need sudo:

- `owner_call.py VERB '<json>'`: one owner verb, answered as JSON.
- `watch.py`: new Self-reflect entries and new issues in the factory's GlitchTip project.
- `pulse.py`: one line on how the factory runs.
- `cpu_sample.py`: one line on the machine's CPU over a minute.
- `limit_watch.py`: Claude usage-limit errors the agents met.

**On the laptop**, from a factory checkout:

- `verify.sh <worktree>`: lint, format, types, every test folder, comment density and the browser drives.
- `density.py <checkout> <ref or WORK> <tree>`: one tree's comment density, as the ratchet counts it.
- `install.py <full sha>`: installs a pushed commit and brings the agents back. It needs `FACTORY_SSH`, the server's
  admin login, and `FACTORY_SCRIPTS`, this folder in the server's install.

A script added to the installed folder takes effect with the install that carries it.
