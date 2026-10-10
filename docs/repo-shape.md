# The repo shape

Every product takes one shape, so the factory needs no per-product configuration. The `repo-shape` check enforces it.

```text
<product>/
├── GOAL.md                 written by a person, never by an agent
├── goal/                   images GOAL.md shows (optional)
├── queue/                  ignored by git: the factory's working folder
├── qa/
│   ├── scenarios/          standing QA scenarios
│   └── fixtures/           inputs scenarios use (optional)
├── check-values/           values the standard checks compare against
├── scripts/
│   └── dev                 the stack, with fixed verbs
├── .github/workflows/      the product's own CI, and its workflow that deploys prod
└── .gitignore              lists queue/
```

## What a product brings for staging and prod

The product keeps its own CI and hosting. The factory only pushes to the default branch, watches the two health
addresses, and starts one workflow when the owner pushes to prod.

- **Two health addresses**, `repo.staging_health` and `repo.prod_health`. Each answers JSON naming the running
  commit as `build_commit`.
- **A CI that deploys staging** on every push to the default branch.
- **One workflow that deploys prod**, named in `repo.promote_workflow`. It runs on `workflow_dispatch` with one input,
  `sha`, and puts that commit on prod. The warden starts it on the default branch when the owner presses Push to prod
  and enters the 6-digit code: with the commit staging runs, or, for a push back, the commit prod ran before. That
  code is the only lock on prod, so the workflow needs no approval step of its own.

## GOAL.md

Plain words and pictures, with four headings:

- `## Who it is for`: the customer, in a paragraph.
- `## The goal`: the owner's goal, then a ranked checklist. Each item reads `- G<n> <outcome>. Done when <check>.`,
  numbered G1, G2 and on in rank.
- `## How it looks and feels`: the look, in words and in images from `goal/`.
- `## How it speaks`: the product's voice, and the words it uses and avoids.

Every role reads it at every start. No agent may change it.

## scripts/dev

One executable. Before each call the factory sets `FACTORY_SLOT`, `FACTORY_PORT` and `FACTORY_SLOT_DIR`; the script
decides everything else about the stack.

| Verb | Does |
| --- | --- |
| `up [service…]` | Builds and starts the slot's stack, or only the named services |
| `down` | Stops it, keeping its data |
| `empty` | Removes the slot's containers, volumes and data |
| `health` | Prints the slot's health URL, which answers JSON naming the running commit as `build_commit` |
| `sign-in <email>` | Prints a one-time sign-in link for a test address |
| `seed save <id> <name>`, `seed load <name> [--as <email>]`, `seed check` | Saved inputs a tester starts from |
| `desktop up <email>`, `desktop down` | A remote desktop for steps outside the web UI, or "not supported" |
| `test [path…]` | Runs the product's tests: all of them, or those under the paths |
| `verbs` | Lists the verbs it supports |
| `reports --since <cursor>` | Optional: prints new user reports as JSON lines |

Everything a slot creates (containers, volumes, networks, images) carries a label naming its slot, so the warden
can find and remove it when the session that held the slot retires.

**User reports.** Without the `reports` verb, the users' inbox stays empty. With it, the warden asks for the reports
after the cursor it last saw, and files each as a request in the users' inbox. Each line is one JSON object with the
report's id (also the next cursor), when it arrived, an id for the user, where in the product it is about, and the
user's words. The words stay in `queue/reports/`; tasks describe them in the factory's own words.

## qa/scenarios

One Markdown file per scenario, with three labels in front matter and at most six steps:

```markdown
---
protects: web/auth/ web/ui/sign-in/
needs: nothing
runs: in-turn
---
```

- `protects`: the paths whose change could break it. A merge that touches one runs it.
- `needs`: what must be set up first, comma-separated: `nothing`, `seed <name>`, `person <role>` (someone the tester
  signs in as; the scenarios README says which test address plays each role) or `session setup` (a step the
  starting session runs).
- `runs`: `in-turn` on the default branch, `desktop` on the slot's remote desktop, or `waiting` (never run until
  what it needs exists).

## check-values/

Only values, never code. Each file is named for the check it feeds (see [checks.md](checks.md)), plus
`surfaces.toml`, the path prefixes that give a diff its QA surfaces. Agents may not change `surfaces.toml`,
`GOAL.md`, `goal/`, `.github/workflows/` or `scripts/ci/`: keep the scripts your workflows run under `scripts/ci/`
to keep them out of agents' reach too. A product names any other path only a person may change, such as a deploy
script kept elsewhere, in `repo.protected_paths` (a trailing `/` names a folder); the guard, the git hooks and the
warden's push refuse a change to it as they do the others.
