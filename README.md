# software-factory

A frozen dev-workflow harness. Pointed at one product repo of the standard shape, it runs the dev cycle with
Claude agents, holds the code to one standard set of checks, ships to staging, and leaves prod to the owner.

- **The warden** is a system service: the only way a session starts, and the keeper of permits, caps, the agent
  lifecycle, probes, cost and settings. Everything the dashboard shows comes from it.
- **The guard** is a Claude Code hook on every tool call of every factory session.
- **The `factory` command** is how agents and the owner ask the warden.
- **The dashboard** is the owner's one window: proposals to approve, staging and prod, the work, the agents,
  spend, the library, settings, self-reflections and the guard.
- **The checks** are one standard set for every product, fed only with values from the product's `check-values/`.
- **The CI template** in `templates/ci/` is every product's CI, copied in unchanged; it calls the product's own
  steps in `scripts/ci/`.
- **The plugin** carries the roles (orchestrator, designers, implementers, intake, triage, critics and testers).

Install (owner only, from a reviewed commit): `sudo python3 runtime/system/install.py --commit <sha> --repo <path>`;
add `--dry-run` first to see every step without changing anything.
How to run it: [docs/operate.md](docs/operate.md). The shape a product must take: [docs/repo-shape.md](docs/repo-shape.md).
