# Self-check

The factory holds every product to the standard checks in `checks/`. This folder holds the factory to the same
checks, on its own code.

Every standard check must pass here, except those that fit only a product, each named with the reason it does not fit
the factory. One rule of the rulebook that no check reads is held here too: every inline ignore names what it ignores
and says why.

Run them before a commit is pushed and installed; a plain test run leaves them out. Install the pinned TypeScript
once with `npm ci`, then run `uv run --frozen --extra dev pytest -m self_check`. The ratchets measure the change
against the commit `FACTORY_CHECK_BASE` names, or else against the factory installed on this machine.
