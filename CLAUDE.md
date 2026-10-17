# software-factory: how people work on it

The factory is a dev-workflow harness: it runs Claude agents on one product repo of the standard shape. It runs on
one server beside the product's prod, and Claude operates it. This file is for the owner and for any Claude session
that works on the factory itself.

- **Claude drives it** through the dashboard's MCP ([docs/mcp.md](docs/mcp.md)): `snapshot` reads the factory,
  Self-reflect included, and `call` runs any owner action. Self-reflect and the `factory` project in the server's
  GlitchTip say what to fix next. Once a fix is installed, mark the entries it addressed fixed with the commit as the
  note (`call reflections.mark`), and those that need no change ignored. SSH to the server as its admin user is the
  way in when the MCP is down: the units are `software-*`, and `factory status` shows the vitals.
- **Agents never edit the factory,** and the factory never works on its own repo. A change is Claude's or the
  owner's: branch, test, merge to main, push to GitHub, then `call factory.upgrade {"commit": "<full sha>"}`. Root's
  upgrade unit installs any commit of the repo on GitHub, so installing the commit before is the way back.
- **Settings change through the factory too:** `call settings.save`. One marked "Next install" takes effect with the
  next upgrade. One marked "Set at install" changes only with a hand install from a clone on the server: pause, then
  `sudo python3 runtime/system/install.py --commit <sha> --repo <the owner's product checkout> --set key=value`,
  then resume.
- **No product here.** No product's name, path, port, host or rule belongs in this repo. A product describes itself
  only in its own `GOAL.md`, and supplies values to the standard checks in its `check-values/`.
- **Every value is a setting.** Caps, models, rounds, times and thresholds live in `settings.toml`, read once
  through the one validated settings object (`runtime/shared/settings.py`). No number or model name in code.
- **Standard library only** in `runtime/`, run as `/usr/bin/python3 -I`, which is Python 3.12: test with
  `uv run --python 3.12 --extra dev`. Only `checks/` may use the pinned tools.
- **Fail loudly.** A missing setting or a malformed file stops with one error listing every problem. Unknown values
  stay unknown: never a zero or a dash for a missing value.
- **The server computes, the dashboard renders.** The UI draws finished values from the warden's API.
- **Tests protect behaviour a caller can see,** and every invariant of the guard and the agent lifecycle has one.
- **Its own checks, too.** The factory meets every standard check that fits a dev tool, with no debt deferred.
  `tests/self_check/` names the product-only checks with their grounds. Before every push, run it on a committed
  clone (`FACTORY_CHECK_BASE=<the installed commit> pytest -m self_check tests/self_check`), and the browser
  drives (`node tests/dashboard_ui/run.cjs`).
- **No mirrored constants.** A value two parts share lives in one module or file that both read, never in a hand copy.
