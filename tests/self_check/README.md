# Self-check

The factory holds every product to the standard checks in `checks/`. This folder holds the factory to the same
checks, on its own code.

Each standard check sits in one of three places here. Most must pass. Some apply but do not pass yet, each named with
the slice of work that brings it to zero. The rest fit only a product, each with the reason it does not fit the
factory.

CI runs these tests on every push, and no release is published unless they pass. A plain local test run leaves them
out.
