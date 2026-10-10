# The repo shape

Every product takes one shape, so the factory needs no per-product configuration. The `repo-shape` and `ci-shape`
checks enforce it.

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
│   ├── dev                 the stack, with fixed verbs
│   └── ci/                 build, smoke, deploy-staging, promote
├── .github/
│   ├── workflows/          the factory's CI template, copied unchanged
│   └── factory-ref         the factory commit CI checks with
└── .gitignore              lists queue/
```

## CI

Every product runs the same CI: the factory's template in `templates/ci/`. The product copies `ci.yml` and
`deploy-prod.yml` into `.github/workflows/` and never edits them; the `ci-shape` check refuses a copy that differs by
one byte. GitHub's free plan cannot share a workflow from a private repo, so the template is copied, never called.
Other workflows, such as the product's own heartbeat, may sit beside the two.

- **`ci.yml`** runs on every push. Its checks job checks out the factory at the commit `.github/factory-ref` pins and
  runs the factory's checks over the whole product. On a push to the default branch it then runs `build`, `smoke` and
  `deploy-staging`, in that order.
- **`deploy-prod.yml`** runs `promote`. The warden starts it when the owner presses Push to prod and enters the
  6-digit code: with the commit staging runs, or, for a push back, the commit prod ran before. That code is the only
  lock on prod.
- A deploy to staging and a deploy to prod never run at once, and neither is cancelled half way.

The product brings the rest:

- **Four executable steps in `scripts/ci/`**: `build`, `smoke`, `deploy-staging` and `promote`. Each is called with
  the commit and decides everything about building and hosting it. Each finds the repo's secrets as one JSON object
  in `CI_SECRETS`, and GitHub's token in `GITHUB_TOKEN`; it may read the code and write packages.
- **`.github/factory-ref`**: one line, `owner/name@<40-character sha>`, naming the factory repo and the commit CI
  checks with. The owner moves it to each factory commit they install.
- **The secret `FACTORY_READ_KEY`**: the private half of a read-only deploy key on the factory repo, so CI can check
  the factory out. The installer makes the pair and prints both halves once; the owner adds the public half to the
  factory repo on GitHub and saves the private half as the product's secret.
- **Two health addresses**, `repo.staging_health` and `repo.prod_health`. Each answers JSON naming the running
  commit as `build_commit`.

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
`surfaces.toml`, the path prefixes that give a diff its QA surfaces.

## What only a person changes

Agents may never change `GOAL.md`, `goal/`, `check-values/surfaces.toml`, `.github/workflows/`, `.github/factory-ref`
or `scripts/ci/`. A product names any other path only a person may change, such as a deploy script its `scripts/ci/`
steps call, in `repo.protected_paths` (a trailing `/` names a folder). The guard, the git hooks and the warden's push
refuse a change to any of them.
