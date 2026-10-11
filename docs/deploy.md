# Deploys

A deploy puts one workspace on the server, staging or prod, on one commit. CI starts it after the commit's images
are built and its smoke test passed. Nothing on the server is built or checked out: a deploy only pulls images.

## How CI reaches the server

CI connects over SSH as the `ci` user. That user's key may run one thing: the gate, `factory-ci-gate`. The gate lets
through exactly three requests and refuses everything else, writing the refusal to the system log:

- `deploy staging <commit> follow`: staging follows the default branch.
- `deploy staging <commit> pin <ref>`: staging shows one branch until it is pinned again. `pin -` unpins it.
- `deploy prod <commit>`.

A commit is 7 to 40 lowercase hex characters; the deploy itself needs at least 8, the length of an image tag. A ref
is letters, digits and `. _ / -`, at most 100, and does not start with `-`. The gate then runs `factory-deploy` as
the admin user. CI sends its registry token on standard input. It reaches the deploy untouched and never lands on
disk outside Docker's own login, which the deploy removes when it ends.

## The steps

Only one deploy runs on a server at a time; a second one waits. Each step stops the deploy if it fails.

1. **Pin.** A `follow` while staging is pinned says so and ends without touching anything. `pin <ref>` records the
   pin; `pin -` removes it. Then the deploy goes on.
2. **Deploy files.** It pulls the commit's deploy image and copies its files out. It refuses the image when its
   commit is not the one asked for, when its `plan.json` has a shape the server's factory does not read, or when
   its `factory.toml` differs from the server's copy in a setting the server set-up acts on (`[product]`,
   `[services]`, `[server]`, each workspace's domain and port, `[deploy] health`, `[edge] routes`). The fix for the
   last two is to run the server set-up again. Every other setting is the build's own: the deploy uses the copy in
   its image, so pushing prod back deploys the older build with the settings it shipped with. The files then go
   into the workspace's stack folder in place: each file is overwritten, never swapped, and a file the commit no
   longer has is deleted. Containers that mount a file keep seeing it.
3. **Settings.** It gives the workspace its share of the shared services (below), then writes the env file.
4. **Images.** It pulls every image the commit's plan names.
5. **Drain.** It asks the running stack's health address how much work is busy. While work is busy it waits, in
   10-second steps, up to `[deploy].drain_minutes`, then goes on with a warning. Nothing answering means nothing
   runs. A healthy answer without a busy count fails the deploy: the product's health answer must report it.
6. **Stack.** It creates any outside network the compose file names, then starts the stack with Docker Compose,
   using only the pulled images.
7. **Edge.** It writes the workspace's site for the server's edge and reloads it.
8. **Answer.** It waits up to `[deploy].answer_seconds` for the public address to answer as this workspace at this
   commit. Another answer fails the deploy, naming what did answer.
9. **After-deploy hooks.** It runs the workspace's hooks from the stack folder, in order. A failing hook fails the
   deploy, but the stack stays live and no image is removed, so a rollback needs no download.
10. **Record.** It records the commit as deployed, adds a line to the history and removes old images.

The last line of a deploy that succeeded reads `deployed <workspace> <commit> previous <commit or ->`.

## The env file

Each workspace has one env file, written from the product's template at every deploy. The template's comments are
kept. For each key:

- `[env.set]` keys are filled in for the workspace: `{workspace}`, `{origin}`, `{port}`, `{commit}`, `{dir}`,
  `{env_dir}`, `{docker_socket}`.
- The factory's service keys (below) are always written. Those the template lacks go in a block at the end.
- Any other key keeps the value the file already holds, so a value set by hand stays.
- A key the template no longer has is removed, and the deploy log says so.
- `generate-with-openssl-rand-hex-<N>` becomes N random bytes in hex, keeping any text before it (`pk-lf-`).
  `generate-with-python-fernet` becomes a Fernet key. Each is made once and kept.

A line `# server: <why>` directly above a key marks a value that is only right on a laptop. A server deploy refuses
while that key is empty or still holds the template's value, and lists every such key with its reason. The file is
written first, so the owner can set those keys in it and deploy again.

The file is private to the admin user and replaced whole on each write. The stack folder holds a link to it.

## What each workspace gets from the shared services

The server runs one set of shared services for every workspace: Postgres, Langfuse and GlitchTip. Each deploy, and
each laptop slot before it runs, makes sure its workspace has:

- **A network** of its own, joined by the three shared services. The product's compose file attaches the services
  that need them to it, through `FACTORY_SERVICES_NETWORK`. Workspaces never share a network.
- **A database and a role** named `<product>_<workspace>`. The role owns the database, and no other workspace's
  role can connect to it. Its password is made once and kept in the workspace's state folder.
- **Error projects** in GlitchTip named `<workspace>-<name>` for each `[services].errors` name.
- **Traces** in the host's one Langfuse project, told apart by environment, which is the workspace's name.

The env keys the factory writes are `FACTORY_SERVICES_NETWORK`, `DATABASE_URL`, `LANGFUSE_HOST`,
`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_TRACING_ENVIRONMENT`, `SENTRY_ENVIRONMENT`, `SENTRY_RELEASE`,
`SENTRY_INGEST_URL` and, for each errors name, `SENTRY_DSN_<NAME>`. With `[services].errors_public_path` set, each
also gets `SENTRY_PUBLIC_DSN_<NAME>`, which reaches GlitchTip through the workspace's own address.

## Staging pins

Staging follows the default branch unless it is pinned. Dispatching CI on another branch pins staging to it and
deploys it; pushes to the default branch then leave staging alone. Dispatching CI on the default branch unpins it.
The pin is the file `pinned` in staging's state folder.

## The deploy log and the history

Each workspace has a state folder, `/srv/<product>/<workspace>/state/`:

- `deploy.log`: every line every deploy printed, each with its time in UTC.
- `history.jsonl`: one line per deploy that answered: when (`at`), the `commit`, the `previous` commit and the
  `mode` (`follow`, `pin <ref>`, or none for prod).
- `deployed`: the commit that runs now.

## Old images

After a deploy succeeds, the server keeps the product's images of the last `[deploy].keep_images` commits of each
workspace, together, and every image a container still uses. It removes the product's other commit-tagged images.
Other images, and tags that are not commits, are never touched. A weekly clean-up removes old build cache and
dangling images ([server.md](server.md)).
