# Checks

The standard checks the factory holds every product to, and the runner that runs them. A product never adds a check; it
only supplies values in its own check-values folder. The factory runs the same checks on its own code through
`tests/self_check/`.
