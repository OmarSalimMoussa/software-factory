# software-factory: how people work on it

The factory is a frozen dev-workflow harness: it runs Claude agents on one product repo of the standard shape.
This file is for the owner and any session the owner opens on the factory itself.

- **Frozen.** Agents never edit the factory, and the factory never works on its own repo. Every change is the
  owner's, made by hand with the factory fully stopped, then reinstalled.
- **No product here.** No product's name, path, port, host or rule belongs in this repo. A product describes itself
  only in its own `GOAL.md`, and supplies values to the standard checks in its `check-values/`.
- **Every value is a setting.** Caps, models, rounds, times and thresholds live in `settings.toml`, read once
  through the one validated settings object (`runtime/shared/settings.py`). No number or model name in code.
- **Standard library only** in `runtime/`, run as `/usr/bin/python3 -I`. Only `checks/` may use the pinned tools.
- **Fail loudly.** A missing setting or a malformed file stops with one error listing every problem. Unknown values
  stay unknown: never a zero or a dash for a missing value.
- **The server computes, the dashboard renders.** The UI draws finished values from the warden's API.
- **Tests protect behaviour a caller can see,** and every invariant of the guard and the agent lifecycle has one.
- **Changes go:** branch, test, merge, push, reinstall. `SELF_REFLECT.md` on the machine says what to change next.
