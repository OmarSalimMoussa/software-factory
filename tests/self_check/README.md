# Self-check

The factory holds every product to the standard checks in `checks/`. This folder holds the factory to the same
checks, on its own code.

Every standard check must pass here, except those that fit only a product, each named with the reason it does not fit
the factory. One rule of the rulebook that no check reads is held here too: every inline ignore names what it ignores
and says why.

CI runs these tests on every push, and no release is published unless they pass. A plain local test run leaves them
out. To run them by hand, install the pinned TypeScript once with `npm ci`, then run
`uv run --frozen --extra dev pytest -m self_check`.
