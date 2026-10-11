# Checks

One standard set of checks, the same for every product, lives in the factory. A product never adds a check of its
own: it only supplies the values a check compares against, in `check-values/`.

## The command

- `factory check` checks what the branch adds; on the default branch, what it adds to the newest commit that passed.
  The warden runs it in a unit of its own, walled into the session's checkout: the product's code it runs (its
  `deps` and its tests) writes only there and in the tools' caches, reads no other worktree, and cannot change the
  shared git folder. A whole run that passes on a committed tree is recorded by the warden, keyed by that tree.
- `factory check --all` checks everything.
- `factory check --only <ids>` runs those checks and records no pass; `factory check --list` prints every check with
  its rule.
- An implementer runs the whole set only on its merge turn, `rounds.check_runs` times a turn. Runs of named checks
  are free.
- A merge whose tree already has a pass is not checked again: the merge hook finds the pass.
- Tests run through the product's `scripts/dev test [path…]`. The factory picks the paths from the diff with
  `check-values/test-map.toml`. On the laptop the whole run stops after `checks.run_minutes`, and the script gets
  `FACTORY_TEST_SECONDS` (`checks.test_seconds`): the product's test runner stops any one test that runs longer.
  The check's unit holds the memory ceiling, `budget.test_memory_ceiling_mib`.
- `tools` runs ruff, mypy and import-linter at the versions the product pins. When the product has a `uv.lock`, the
  check first syncs the product's environment (`uv sync --frozen --all-extras`); a failed sync fails the check. Each
  tool then runs from the product's `.venv`, so mypy loads the product's own plugins. A tool the product does not
  pin runs at the factory's version.
- A Python file the product's ruff configuration excludes (`exclude` or `extend-exclude` under `[tool.ruff]`) is not
  code: no check parses or measures it. A file that is not excluded and does not parse fails each check that reads
  it, with one line naming it.

## Values

Each values file is named for the check it feeds and holds only values, checked against that check's schema. A
check that reads no values needs no file.

| Check | Values file |
| --- | --- |
| `tools` | the product's `pyproject.toml` |
| `tests` | `test-map.toml` |
| `comment-budgets` | `comment-budgets.toml` |
| `error-telemetry` | `error-telemetry.toml` |
| `mirror-markers`, `mirrored-constants` | `mirror-markers.toml`, `mirrored-constants.toml` |
| `tree-rules`, `doc-refs`, `no-console` | `tree-rules.toml`, `doc-refs.toml`, `no-console.toml` |
| `network-door`, `config-readers`, `js-import-rules` | `network-door.toml`, `config-readers.toml`, `js-import-rules.toml` |
| `vocabulary-homes`, `computation-owners`, `quiet-modules` | the file of the same name, `.toml` |
| `image-sources`, `env-template-sync`, `pinned-config`, `generated-current` | the file of the same name, `.toml` |
| `size-ceilings`, `comment-budgets` (their pins) | `baseline.json` |

`repo-shape`, `deploy-shape`, `testing-imports`, `qa-scenarios`, `seeds-current` and `queue-format` read no
values. `deploy-shape` reads the product's `factory.toml` instead: it holds the product to the standard build and
deploy, from the workflows and the hooks to the images, the deploy files and the ports
([repo-shape.md](repo-shape.md)).

## Ratchets and pins

A ratchet (`complexity-ratchet`, `count-claims`, `history-narrative`, and the docstring and density budgets of
`comment-budgets`) compares each file or tree a branch changed with how it stands on the default branch, measured
the same way: only getting worse fails, and no number is kept by hand, so an improvement tightens the baseline as
soon as it merges and parallel branches never conflict over one. Checking everything (`--all`) runs no ratchet.
Growing past a ceiling needs a pin in `baseline.json` with its grounds in one line; a pin without grounds fails.

## When a check fails

Trust its message: each failure names the rule, the file and what to do. Fix the code, never the values, unless the
values are what is wrong; a change to `check-values/` is reviewed like code. `surfaces.toml` is the owner's alone.
