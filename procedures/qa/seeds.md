# Seeds

A seed is saved input that a tester starts from, so a check does not wait for data to be made by hand. The product's
`scripts/dev` decides what a seed holds; the factory only names the verbs. Implementers and the orchestrator run
them, for their own slot; testers never do.

- `factory slot seed load <name> [--as <address>]` loads a seed into the slot, for a test address when one is named.
  Load it as often as needed: each load is new data.
- `factory slot seed save <id> <name>` keeps something made on the slot as a seed.
- `factory slot seed check` tests every seed against today's formats, without a stack. The `seeds-current` check runs
  it, so a format change cannot merge while a seed fails.

Rules:

- A scenario's `needs` label names the seeds it needs; whoever starts the tester loads them first.
- A change to a format a seed holds converts every seed in the same branch. Nothing the seed captured changes; a
  part it lacks is left empty, and its title says what it lacks.
- Never make a seed again from scratch without the owner's say-so, asked through the orchestrator.
