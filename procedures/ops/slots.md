# Slots

A slot is one running copy of the product on the laptop. The factory decides who holds which; the product's
`scripts/dev` decides what a stack is. Every command works out your slot from your lease, never from an argument.

| Command | Who | Does |
| --- | --- | --- |
| `factory slot claim` | an implementer | Takes a free slot (1 and up) and records you as its holder |
| `factory slot up [service…]` | the holder | Builds and starts the slot's stack, or only the named services. A running service whose image is unchanged keeps running. An up that would change nothing (the same checkout, env file and words as the slot's last good up, with every container it left still running) runs nothing and says so; `down` first to build again. Slot 0, which is staging, comes up whole |
| `factory slot restart <service…>` | the holder | Restarts the named services, which must be running |
| `factory slot down` | the holder | Stops it, keeping its data |
| `factory slot run <service> -- <command…>` | the holder and its backend and security testers | Runs one command in a new container of the service and answers with its output. Bring the service up first, so the run is not spent building it |
| `factory slot release` | the holder | Gives the slot back once your work is checked |
| `factory slot free <n>` | the orchestrator | Frees a slot whose work is finished |
| `factory slot list` | anyone | Who holds which slot, since when, and why |
| `factory slot top` | the holder and its testers | The CPU and memory of each container running for the slot, the product's own run-time ones included, and the CPU, run time and name of each process in it. Quote it to prove a change costs less CPU |
| `factory slot sign-in`, `seed …`, `desktop …` | as their procedures say | `qa/sign-in.md`, `qa/seeds.md`, `qa/desktop.md` |

- **Slot 0** belongs to the main checkout: the orchestrator's check of the default branch.
- **Leases do not expire.** Only the holder may bring a slot up, down or empty it. A stack command before a claim is
  refused rather than falling back to slot 0. When a session retires (finished, stopped, crashed) or is benched, the
  warden empties its slot and gives it up itself; a slot that would not empty is tried again.
- **No change under a tester.** While a tester you started still walks the slot, `up`, `restart` and `down` are
  refused: change the stack once it reports, so no tester sees the slot rebuilt mid-step.
- **Output.** The warden answers with the command's exit status and its last lines, or a run's whole output, up to
  `slots.tail_max_kib`; the full log is kept for the owner. A product whose `scripts/dev` has no `run` or `restart`
  says so.
- **Memory.** A test run starts only when free memory covers its ceiling and the reserve. Hitting the cap is a leak to
  fix, not a reason to raise it.
