# software-factory

A frozen dev-workflow harness. Pointed at one product repo of the standard shape, it runs the dev cycle with
Claude agents, holds the code to one standard set of checks, ships to staging, and leaves prod to the owner.

- **The warden** is a system service: the only way a session starts, and the keeper of permits, caps, the agent
  lifecycle, probes, cost and settings. Everything the dashboard shows comes from it.
- **The guard** is a Claude Code hook on every tool call of every factory session.
- **The `factory` command** is how agents and the owner ask the warden.
- **The dashboard** is the owner's one window: proposals to approve, staging and prod, the work, the agents,
  spend, the library, settings (the agents' Claude account among them), self-reflections and the guard.
- **The checks** are one standard set for every product, fed only with values from the product's `check-values/`.
- **The plugin** carries the roles (orchestrator, designers, implementers, intake, triage, critics and testers).
- **The standard build and deploy** in `standard/`: the build plan, the server's set-up, prod's build and deploy on
  the server, as the product's `factory.toml` says, and the shared services every product gets (a database, traces
  and errors), which the factory's slots use too.

Set up the product's server first, from the owner's laptop: `python3 -m standard.server.setup --product <path>`
([docs/server.md](docs/server.md)). Then install the factory from a clone of its repo, with your GitHub CLI signed in:

```
gh repo clone OmarSalimMoussa/software-factory && cd software-factory
sudo python3 runtime/system/install.py --commit HEAD --repo <your checkout of the product>
```

The install seals the commit into a read-only copy and runs every step from it, in the terminal, as a few phases. It
asks for each secret in place, and every command and its output goes to a log in `/var/log/software-factory/`. After
that, the owner action `factory.upgrade` installs any commit of the factory's repo on GitHub, through root's upgrade
unit; installing the commit before is the way back. `runtime/system/uninstall.py` removes the factory. The install
runs on Debian or Ubuntu, and checks everything it needs before it changes anything. It copies the programs the
product's scripts need (the setting `install.toolchain`) as your own shell finds them, rc files and all (nvm, mise,
fnm), and refuses one whose version differs from the product's `.nvmrc` or `.node-version`. It copies the Python the
product asks for from your uv, so the agents and the stack run the same tools you do. It puts the dashboard on the
server's edge, at each name in `dashboard.hosts` that is not loopback (give yours with `--set`), and refuses a machine
with no edge. With `slots.zero_origin` set, it opens staging there too, on the box's own address. The uninstall takes
both off the edge again, and leaves what the server set-up placed.

How to run it: [docs/operate.md](docs/operate.md). The shape a product must take: [docs/repo-shape.md](docs/repo-shape.md).
