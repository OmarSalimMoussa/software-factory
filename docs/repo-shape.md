# The repo shape

Every product takes one shape, so the factory needs nothing from a product beyond its own settings file,
`factory.toml`. The `repo-shape` and `deploy-shape` checks enforce the shape.

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
├── factory.toml            how the product is built, smoke-tested and deployed
├── compose.yml             the stack every workspace and laptop slot runs
├── <env template>          the settings the factory fills in for each workspace and slot
├── .github/
│   ├── workflows/          the factory's two workflows, copied unchanged
│   └── factory-ref         the factory commit CI checks with
└── .gitignore              lists queue/
```

## factory.toml

The product's one settings file for the standard build and deploy. Every key is required unless marked optional,
an unknown key is an error, and every problem is reported at once. The factory's `standard/config.py` is the
schema.

| Section | Says |
| --- | --- |
| `[product]` | The product's short name, and the registry its images go to: `ghcr.io/<owner>`, where CI signs in |
| `[build]` | The build arguments that carry the commit, and the images no compose service builds |
| `[smoke]` | Where the smoke test finds the health address, what else must answer, and the product's own check |
| `[services]` | The owner's sign-in on the shared services, and the error projects each workspace gets |
| `[env]` | The env file compose reads, its template, and the keys the factory writes into it |
| `[server]` | The server: its host and pinned key, its admin user, its tailnet name, the certificate email |
| `[workspaces.staging]`, `[workspaces.prod]` | Each workspace's domain, loopback port and after-deploy scripts |
| `[deploy]` | The health address, how long a deploy waits, the files a deploy copies, how many builds to keep |
| `[edge]` | Paths the edge answers 404, and hosts it redirects |
| `[[release]]` | Optional: what to release after a prod deploy, such as a browser extension |

The scripts factory.toml names are its **hooks**: `[smoke].check` runs in CI after the smoke test, each workspace's
`after_deploy` scripts run on the server after a deploy, and each release's `package` runs in CI after a prod
deploy. Each hook is committed and executable.

## compose.yml

- **Every service with a `build:` section is one of the product's images**, named after the service:
  `<registry>/<name>-<service>`. Its `image:` is that name tagged `${KEY}`, where `[env.set]` sets `KEY` to
  `"{commit}"`, so a deploy runs the image CI built for the commit. An image no service runs goes in
  `[build].extra_images`.
- **A Dockerfile may build FROM another product image** tagged with `[build].tag_arg`. It then builds after that
  image. One level only: a base image never builds from a third.
- **Build arguments are literals**, or name `[build].commit_arg` or `[build].tag_arg`, which CI fills with the
  commit. CI has no product env to fill anything else.
- **Every published port binds 127.0.0.1.** The edge is the only way in.
- **A service that uses the shared services** (Postgres, Langfuse, GlitchTip) joins the external network named
  `${FACTORY_SERVICES_NETWORK}`, which the factory creates for each workspace and slot.
- **`[deploy].files` lists what a deploy copies to the server**: compose.yml, the env template, and every file a
  service mounts from the repo. A folder there is copied whole.

## CI

Every product runs the same two workflows, the factory's `templates/ci/`, copied into `.github/workflows/` byte
for byte: `ci.yml` checks every push and takes the default branch to staging, and `deploy-prod.yml` deploys prod
when the owner pushes. The server, not CI, tells the uptime monitor prod is up ([server.md](server.md)).
[ci.md](ci.md) says what each workflow does and [deploy.md](deploy.md) what a deploy does on the server. The product
brings:

- **`.github/factory-ref`**: one line, `owner/name@<40-character sha>`, naming the factory repo and the commit CI
  checks and deploys with. The owner moves it to each factory commit they install.
- **The secret `FACTORY_READ_KEY`**: the private half of a read-only deploy key on the factory repo, so CI can check
  the factory out. The installer makes the pair and prints both halves once. [ci.md](ci.md) lists the other secrets.
- **A health address**, `[deploy].health`, which answers JSON naming the `workspace`, the running commit as
  `build_commit`, and how much work is in flight as `busy`.

## GOAL.md

Plain words and pictures, with four headings:

- `## Who it is for`: the customer, in a paragraph.
- `## The goal`: the owner's goal, then its items, top ranked first. Each reads `- <name> <outcome>. Done when
  <check>.`, the name a stable one such as `G-build-sop`: tasks name their item by it, so moving a line reranks it
  and never points a task at another item. Mark an item done with `- [x] <name> …`: it keeps its name, and ranks no
  more.
- `## How it looks and feels`: the look, in words and in images from `goal/`.
- `## How it speaks`: the product's voice, and the words it uses and avoids.

Every role reads it at every start. No agent may change it.

## scripts/dev

One executable. Before each call the factory sets these variables; the script decides everything else about the
stack.

- `FACTORY_SLOT`: the slot's number.
- `FACTORY_PORT`: the loopback port the slot's stack publishes.
- `FACTORY_SLOT_DIR`: the slot's folder in the factory's state, which the script may read.
- `FACTORY_ENV_FILE`: the slot's env file. The factory writes it before each call with every key the product's
  compose reads: the template's keys, `[env.set]`, and the shared services' keys. Hand it to compose as its env file.

| Verb | Does |
| --- | --- |
| `up [service…]` | Builds and starts the slot's stack, or only the named services |
| `down` | Stops it, keeping its data |
| `empty` | Removes the slot's containers, volumes and data |
| `health` | Prints the slot's health URL, which answers JSON naming the running commit as `build_commit` |
| `sign-in <email>` | Prints a one-time sign-in link for a test address |
| `seed save <id> <name>`, `seed load <name> [--as <email>]`, `seed check` | Saved inputs a tester starts from |
| `desktop up <email>`, `desktop down` | A remote desktop for steps outside the web UI, or "not supported" |
| `test [path…]` | Runs the product's tests: all of them, or those under the paths. When `FACTORY_TEST_SECONDS` is set, it stops any one test that runs longer and fails it |
| `verbs` | Lists the verbs it supports |
| `deps` | Optional: installs what the checks and tests read (package trees, tools); `factory check` runs it first |
| `reports --since <cursor>` | Optional: prints erased reports, then new user reports, as JSON lines |

Everything a slot creates (containers, volumes, networks, images) carries a label naming its slot, so the warden
can find and remove it when the session that held the slot retires. A container the stack starts while it runs, such
as an agent's, needs no label: the warden also removes every container still on one of the slot's networks.

**User reports.** Without the `reports` verb, the users' inbox stays empty. With it, the warden asks for the reports
after the cursor it last saw, and files each as a request in the users' inbox. Each line is one JSON object with the
report's id (also the next cursor), when it arrived, an id for the user, where in the product it is about, and the
user's words. The words stay in `queue/reports/`; tasks describe them in the factory's own words.

**Erased reports.** Before its report lines, `reports` prints one line for each report a user erased:
`{"erased": "<report id>", "at": "YYYY-MM-DDTHH:MM:SSZ"}`, with no cursor. The factory then deletes that report's
words, once. Its request keeps its id and says the user erased it. The factory remembers every erasure, so a report
erased before the factory pulled it is never filed.

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

Agents may never change `GOAL.md`, `goal/`, `check-values/surfaces.toml`, `.github/workflows/`,
`.github/factory-ref`, `factory.toml` or any hook it names. The hooks are read from the factory's own checkout of the
product, so a branch cannot drop one from the list. A product names any other path only a person may change in
`repo.protected_paths` (a trailing `/` names a folder). The guard, the git hooks and the warden's push refuse a change
to any of them. While that checkout's `factory.toml` cannot be read, they refuse every write, commit and push, until
the owner fixes it.
