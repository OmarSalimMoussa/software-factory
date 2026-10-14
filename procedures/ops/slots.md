# Slots

A slot is one running copy of the product on the laptop. The factory decides who holds which; the product's
`scripts/dev` decides what a stack is. Every command works out your slot from your lease, never from an argument.

| Command | Who | Does |
| --- | --- | --- |
| `factory slot claim` | an implementer | Takes a free slot (1 and up) and records you as its holder |
| `factory slot up [service…]` | the holder | Builds and starts the slot's stack, or only the named services |
| `factory slot down` | the holder | Stops it, keeping its data |
| `factory slot run <service> -- <command…>` | the holder | Runs one command in a new container of the service and answers with its output. Bring the service up first, so the run is not spent building it |
| `factory slot release` | the holder | Gives the slot back once your work is checked |
| `factory slot free <n>` | the orchestrator | Frees a slot whose work is finished |
| `factory slot list` | anyone | Who holds which slot, since when, and why |
| `factory slot sign-in`, `seed …`, `desktop …` | as their procedures say | `qa/sign-in.md`, `qa/seeds.md`, `qa/desktop.md` |

- **Slot 0** belongs to the main checkout: the orchestrator's check of the default branch.
- **Leases do not expire.** Only the holder may bring a slot up, down or empty it. A stack command before a claim is
  refused rather than falling back to slot 0. When a session retires (finished, stopped, crashed) or is benched, the
  warden empties its slot and gives it up itself; a slot that would not empty is tried again.
- **Output.** The warden answers with the command's exit status and its last lines, or a run's whole output, up to
  `slots.tail_max_kib`; the full log is kept for the owner. A product whose `scripts/dev` has no `run` says so.
- **Memory.** A test run starts only when free memory covers its ceiling and the reserve. Hitting the cap is a leak to
  fix, not a reason to raise it.
