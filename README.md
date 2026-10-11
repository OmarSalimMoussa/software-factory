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
- **The CI templates** in `templates/ci/` are every product's CI, copied in unchanged. With the standard build and
  deploy in `standard/`, they build each push, smoke-test it and deploy it, as the product's `factory.toml` says.
- **The plugin** carries the roles (orchestrator, designers, implementers, intake, triage, critics and testers).

- **The standard build and deploy** in `standard/`: the build plan and smoke test CI runs, the server's set-up and
  deploy command, and the shared services every product gets (a database, traces and errors), on the server and on
  the laptop.

Set up the product's server first, from the owner's laptop: `python3 -m standard.server.setup --product <path>`
([docs/server.md](docs/server.md)). Install (owner only, from a reviewed commit):
`sudo python3 runtime/system/install.py --commit <sha> --repo <path>`; add `--dry-run` first to see every step without
changing anything. The install copies the programs the product's scripts need (the setting `install.toolchain`) from
your PATH, and the Python the product asks for from your uv, so the agents and the stack run the same tools you do.
How to run it: [docs/operate.md](docs/operate.md). The shape a product must take: [docs/repo-shape.md](docs/repo-shape.md).
