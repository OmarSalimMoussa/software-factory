# The repo shape

<!-- doc-refs: external -->

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
├── factory.toml            how the product is built and deployed
├── compose.yml             the stack prod and every slot runs
├── <env template>          the settings the factory fills in for prod and each slot
└── .gitignore              lists queue/
```

## factory.toml

The product's one settings file for the standard build and deploy. Every key is required unless marked optional,
an unknown key is an error, and every problem is reported at once. The factory's `standard/config.py` is the
schema.

| Section | Says |
| --- | --- |
| `[product]` | The product's short name, and the registry path its image names start with |
| `[build]` | The build arguments that carry the commit, and the images no compose service builds |
| `[services]` | The owner's sign-in on the shared services, and the error projects each workspace gets |
| `[env]` | The env file compose reads, its template, and the keys the factory writes into it |
| `[server]` | The server: its host and pinned key, its admin user, its tailnet name, the certificate email |
| `[workspaces.prod]` | Prod's domain, loopback port and after-deploy scripts |
| `[deploy]` | The health address, how long a deploy waits, the files a deploy copies, how many builds to keep |
| `[edge]` | Paths the edge answers 404, and hosts it redirects |
| `[reports]` | Optional: the tailnet port and path where prod's report export answers, the header that carries its token, and `token_key`: the key in prod's env file that holds the token |
| `[records]` | Optional: `folders`, the folders of prod's volumes whose files agents may read with `factory records`, each the compose volume's name and then folders in it, such as `job-store/*/steps`, where `*` stands for any one folder |

The scripts factory.toml names are its **hooks**: prod's `after_deploy` scripts, which run on the server after a
deploy. Each hook is committed and executable.

## compose.yml

- **Every service with a `build:` section is one of the product's images**, named after the service:
  `<registry>/<name>-<service>`. Its `image:` is that name tagged `${KEY}`, where `[env.set]` sets `KEY` to
  `"{commit}"`, so a deploy runs the image it built for the commit. An image no service runs goes in
  `[build].extra_images`.
- **A Dockerfile may build FROM another product image** tagged with `[build].tag_arg`. It then builds after that
  image. One level only: a base image never builds from a third.
- **Build arguments are literals**, or name `[build].commit_arg` or `[build].tag_arg`, which the build fills with
  the commit. The build has no product env to fill anything else.
- **Every image leaves out `**/.claude`** in its ignore file: the Dockerfile's own `<Dockerfile>.dockerignore` when
  it has one, else its context's `.dockerignore`. Agents' worktrees live there, and Claude Code leaves folders there
  that the user building the slots cannot read.
- **Every published port binds 127.0.0.1.** The edge is the only way in.
- **Every service says `restart: unless-stopped`.** Docker then brings back a service that stopped on its own and
  counts it, and the factory reports each one it counts ([shared-services.md](shared-services.md)).
- **A service that uses the shared services** (Postgres, GlitchTip) joins the external network named
  `${FACTORY_SERVICES_NETWORK}`, which the factory creates for each workspace and slot.
- **`[deploy].files` lists what a deploy copies into prod's stack folder**: compose.yml, the env template, and every
  file a service mounts from the repo. A folder there is copied whole.

## The build and deploy

The factory builds and deploys prod on the server, from a clean copy of the commit the owner pushes
([deploy.md](deploy.md)), so a product needs no CI. GitHub holds a mirror of the default branch, which the warden
keeps up to date. GitHub refuses the warden's deploy key a push that adds or edits a file in `.github/workflows/`,
and the mirror then stays where it was until the owner pushes with their own login, so only a person changes those
files. The server, not GitHub, tells the uptime monitor prod is up ([server.md](server.md)).

The product brings a **health address**, `[deploy].health`, which answers JSON naming the `workspace`, the running
commit as `build_commit`, and how much work is in flight as `busy` ([deploy.md](deploy.md) says what each holds).

## GOAL.md

Plain words and pictures, with four headings:

- `## Who it is for`: the customer, in a paragraph.
- `## The goal`: the owner's goal, then its items, top ranked first. Each reads `- <name> <outcome>. Done when
  <check>.`, the name a stable one such as `G-build-sop`: tasks name their item by it, so moving a line reranks it
  and never points a task at another item. Mark an item done with `- [x] <name> …`: it keeps its name, and ranks no
  more. Adding, removing, rewording, moving or marking an item brings all parked work back for triage.
- `## How it looks and feels`: the look, in words and in images from `goal/`.
- `## How it speaks`: the product's voice, and the words it uses and avoids.

Every role reads it at every start. No agent may change it.

## scripts/dev

One executable. Before each call the factory sets these variables; the script decides everything else about the
stack.

- `FACTORY_SLOT`: the slot's number.
- `FACTORY_PORT`: the loopback port the slot's stack publishes.
- `FACTORY_SLOT_DIR`: the slot's folder in the factory's state, which the script may read.
- `FACTORY_RUN_LABEL`: a label, `key=value`, for the container `run` starts, so the factory can find it.
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
| `desktop up <email>`, `desktop down` | A remote desktop for steps outside the web UI, or "not supported". Its browser reaches the slot at `127.0.0.1` on `FACTORY_PORT`, as a tester's browser does, so a sign-in link the factory prints opens there |
| `test [path…]` | Runs the product's tests: all of them, or those under the paths. When `FACTORY_TEST_SECONDS` is set, it stops any one test that runs longer and fails it; when `FACTORY_TEST_WORKERS` is set, it runs at most that many test processes at once |
| `verbs` | Lists the verbs it supports |
| `deps` | Optional: installs what the checks and tests read (package trees, tools); `factory check` runs it first |
| `reports --since <cursor>` | Optional: reads prod's report export on its input, and prints erased reports, then new user reports, as JSON lines |
| `run <service> -- <command…>` | Optional: runs one command in a new container of the service, labelled `FACTORY_RUN_LABEL`, with the slot's settings and network, and prints its output. The factory stops it at `slots.run_seconds` and removes its container |
| `restart <service…>` | Optional: restarts the named services' running containers, as they are |
| `stop <service…>` | Optional: stops the named services' containers, keeping their data |

Everything a slot creates (containers, volumes, networks, images) carries a label naming its slot, so the warden
can find and remove it when the session that held the slot retires. A container the stack starts while it runs, such
as an agent's, needs no label: the warden also removes every container still on one of the slot's networks.

**User reports.** Without the `reports` verb, the users' inbox stays empty. With it, the warden reads prod's report
export itself, where the server set-up saved it from `[reports]`, so no code agents wrote reaches the tailnet. It hands
the export to `reports` on its input, with the cursor it last saw, and files each report the verb prints as a request
in the users' inbox. Each line is one JSON object with the report's id (also the next cursor), when it arrived, an id
for the user, where in the product it is about, and the user's words. The words stay in `queue/reports/`; tasks describe them in the factory's own words.

**Erased reports.** Before its report lines, `reports` prints one line for each report a user erased:
`{"erased": "<report id>", "at": "YYYY-MM-DDTHH:MM:SSZ"}`, with no cursor. The factory then deletes that report's
words, once. Its request keeps its id and says the user erased it. The factory remembers every erasure, so a report
erased before the factory pulled it is never filed.

## qa/scenarios

One Markdown file per scenario, with three labels in front matter, an optional fourth, and at most `qa.max_steps`
numbered steps for each item it takes:

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
  starting session runs). A scenario that signs in as a named address lists a `person`. One with two people at
  once, such as one inviting the other, lists a `person` for each: the tester signs the second in on its second
  browser, which keeps its own cookies and storage.
- `runs`: `in-turn` on the default branch, `desktop` on the slot's remote desktop, or `waiting` (never run until
  what it needs exists).
- `items` (optional): how many of a tester's items it takes, from 1 to `qa.max_items`; without it, one. A tester's
  time grows with its items, on the remote desktop too, so a scenario with a long wait, such as a recording or a
  rebuild, names more.

A scenario's full pass carries to a later check while nothing it depends on changes
([dev-cycle.md](dev-cycle.md)), so a path every scenario depends on that no `protects` names, such as a lockfile, the
app's entry point or a shared stylesheet, belongs in `check-values/qa-shared.toml`: `paths = ["uv.lock", "web/app.py"]`.
Each must exist. A change to one carries no pass past it; without the file, no pass carries.

## check-values/

Only values, never code. Each file is named for the check it feeds (see [checks.md](checks.md)), plus
`surfaces.toml`, the path prefixes that give a diff its QA surfaces, and `qa-shared.toml`, the paths every scenario
shares.

## What only a person changes

Agents may never change `GOAL.md`, `goal/`, `check-values/surfaces.toml`, `.github/workflows/`, `factory.toml` or
any hook it names. The hooks factory.toml names are read from the factory's own checkout of the product, so a branch
cannot drop one from the list. A product names any other path only a person may change in `repo.protected_paths`
(a trailing `/` names a folder). The guard and the git hooks refuse an agent's change to any of them. While that
checkout's `factory.toml` cannot be read, they refuse every write and commit, until the owner fixes it.

To change one yourself, commit on the agents' default branch in that checkout, as the agent user:
`sudo -u software-agent git -C <repo.path> …` ([operate.md](operate.md) gives the steps). The warden then asks the
orchestrator to check that branch. Once the check passes, slot 0 runs your change as staging, and it goes to prod
with the next Push to prod.
