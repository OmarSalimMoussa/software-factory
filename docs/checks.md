# Checks

One standard set of checks, the same for every product, lives in the factory. A product never adds a check of its
own: it only supplies the values a check compares against, in `check-values/`.

## The command

- `factory check` checks what the branch adds, and records a pass keyed by the git tree when nothing was skipped.
- `factory check --all` checks everything.
- `factory check --only <ids>` runs those checks; `factory check --list` prints every check with its rule.
- A merge whose tree already has a pass is not checked again: the merge hook finds the pass.
- Tests run through the product's `scripts/dev test [path…]`. The factory picks the paths from the diff with
  `check-values/test-map.toml`, and each test must finish within 45 seconds.

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
| `image-roster`, `image-sources`, `deploy-bundle` | the file of the same name, `.toml` |
| `env-template-sync`, `pinned-config`, `generated-current` | the file of the same name, `.toml` |
| `size-ceilings`, `complexity-ratchet`, `count-claims`, `history-narrative` | `baseline.json` |

`repo-shape`, `ci-shape`, `testing-imports`, `qa-scenarios`, `seeds-current` and `queue-format` read no
values.

## Ratchets and pins

`baseline.json` records where a file stands today on the ratchets: its size, its complexity, its comment density.
Only getting worse fails, so an improvement never needs a re-run. Growing past a ceiling needs a pin in
`baseline.json` with its grounds in one line; a pin without grounds fails.

## When a check fails

Trust its message: each failure names the rule, the file and what to do. Fix the code, never the values, unless the
values are what is wrong; a change to `check-values/` is reviewed like code. `surfaces.toml` is the owner's alone.
