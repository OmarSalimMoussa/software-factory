# Deploys

A deploy puts prod on one commit. The owner starts it with Push to prod, or with Push back to the commit prod ran
before. It builds the commit's images on the server, from a clean copy of the commit, and never at `up`.

## How the warden starts it

The warden cannot run Docker itself. It writes the request, the full commit and whether it is a push back, to
`public/launch/<id>.json` and starts `software-deploy-run@<id>.service`. That unit runs the deploy as the server's
admin user, who owns prod's folders and the edge's sites and is in the docker group. The unit is outside the factory's
slice and bound to no warden, so an install or a warden restart never stops a deploy half way, and it has no time
limit of its own: the deploy fits itself into `watch.push_wait_minutes` (step 3). Its output, every line of which the
deploy also keeps in its log, and its exit status go back to the warden. A failure's line starts `failed:`.

## The steps

Only one deploy runs on a server at a time; a second one waits. Each step stops the deploy if it fails.

1. **Settings.** The deploy checks that the warden's mirror holds the commit. A push to prod reads the commit's
   `factory.toml`; one that does not parse fails the push, naming why, and prod stays as it is, as it does when the
   server's copy, `/etc/software-factory/product.toml`, differs and is not the admin user's to rewrite. A push back
   deploys with the server's copy as it is: the factory may not parse an older commit's `factory.toml`.
2. **Source.** It writes the commit's files from the mirror into a new folder beside prod's stack.
3. **Images.** It plans the build with those settings and builds each image of the plan that Docker lacks at the
   commit, base images first, passing only the plan's build args. A push back usually builds nothing. No image gets a
   label, so the warden's clean-up of the slots' images never reaches prod's, and nothing is pushed anywhere. A build
   that leaves less of `watch.push_wait_minutes` than `[deploy].drain_minutes` and `answer_seconds` need fails the
   deploy, and prod stays as it is.
4. **Deploy files.** It copies the plan's files, `[deploy].files` with every after-deploy hook and `factory.toml`, into
   prod's stack folder in place: each file is overwritten, never swapped, and a file the commit no longer has is
   deleted. Containers that mount a file keep seeing it. A link, or the env file itself, among them fails the deploy.
5. **Env file.** It gives prod its share of the shared services (below), then writes the env file.
6. **Drain.** It asks the running stack's health address how much work is busy. While work is busy it waits, in
   10-second steps, up to `[deploy].drain_minutes`, then goes on with a warning. Nothing answering means nothing
   runs. A healthy answer without a busy count fails the deploy: the product's health answer must report it.
7. **Stack.** It creates any outside network the compose file names, then starts the stack with Docker Compose,
   using only the images it holds. A service whose mounted file changed restarts.
8. **Edge.** It writes prod's site for the server's edge and reloads it.
9. **Answer.** It waits up to `[deploy].answer_seconds` for the public address to answer as `prod` at this commit.
   Another answer fails the deploy, naming what did answer.
10. **After-deploy hooks.** It runs prod's hooks from the stack folder, in order. A failing hook fails the deploy, but
    the stack stays live and no image is removed, so a push back builds nothing.
11. **Record.** It records the commit as deployed, adds a line to the history and removes old images. A push to prod
    then makes the server's `product.toml` the commit's `factory.toml`, so it always holds what prod runs.

The last line of a deploy that succeeded reads `deployed prod <commit> previous <commit or ->`.

## What the product must answer

`[deploy].health`, in prod and in every slot, answers status 200 with a JSON object:

- `workspace`: the workspace the stack runs as: `prod`, or `slot<n>` in a slot, so `slot0` on staging. The product
  reads it from its env file: `[env.set]` can write `{workspace}` into any key.
- `build_commit`: the commit the running image was built from. In prod it is the commit's first 8 characters, which
  the build argument `[build].commit_arg` carries.
- `busy`: a whole number, the work in flight. A deploy waits while it is above 0, up to `[deploy].drain_minutes`.

The deploy's drain and answer steps read it, and so do the warden's probes of prod and staging.

## The env file

Prod has one env file, written from the product's template at every deploy. The template's comments are
kept. For each key:

- `[env.set]` keys are filled in for the workspace: `{workspace}`, `{origin}`, `{port}`, `{commit}`, `{dir}`,
  `{env_dir}`, `{docker_socket}`.
- The factory's service keys (below) are always written. Those the template lacks go in a block at the end.
- Any other key keeps a value set by hand. A key that still holds the default the template had at the last write
  takes the template's current default, so a changed default reaches prod and every slot. The defaults
  of the last write are kept beside the file, in `<file>.defaults`.
- A key the template no longer has is removed, and the deploy log says so.
- `generate-with-openssl-rand-hex-<N>` becomes N random bytes in hex, keeping any text before it, such as `pk-`.
  `generate-with-python-fernet` becomes a Fernet key. Each is made once and kept.

A line `# server: <why>` directly above a key marks a value that is only right on a laptop. A server deploy refuses
while that key is empty or still holds the template's value or any part of it, and lists every such key with its
reason. Each comma-separated item counts on its own, with quotes, spaces and letter case set aside, so
`'QA@notes.test'` still holds the laptop's `admin@notes.test,qa@notes.test`. It refuses
any key, marked or not, that holds a placeholder: `REPLACE_ME` in its value, or one `<name>`. The file is written
first, so the owner can set those keys in it and deploy again.

The file is private to the admin user and replaced whole on each write. The stack folder holds a link to it.

## What each workspace gets from the shared services

The server runs one set of shared services for every workspace: Postgres and GlitchTip. Each deploy, and each
laptop slot before it runs, makes sure its workspace has:

- **A network** of its own, joined by both shared services. The product's compose file attaches the services that
  need them to it, through `FACTORY_SERVICES_NETWORK`. Workspaces never share a network.
- **A second network**, `FACTORY_TELEMETRY_NETWORK`, joined by GlitchTip alone. What the product runs for others,
  such as its agents' containers, joins it instead, so that code can report errors but never reach the database.
- **A database and a role** named `<product>_<workspace>`. The role owns the database, and no other workspace's
  role can connect to it. Its password is made once and kept in the workspace's state folder.
- **Error projects** in GlitchTip named `<workspace>-<name>` for each `[services].errors` name.

The env keys the factory writes are `FACTORY_SERVICES_NETWORK`, `FACTORY_TELEMETRY_NETWORK`, `DATABASE_URL`,
`SENTRY_ENVIRONMENT`, `SENTRY_RELEASE`, `SENTRY_INGEST_URL` and, for each errors name, `SENTRY_DSN_<NAME>`. With
`[services].errors_public_path` set, each also gets `SENTRY_PUBLIC_DSN_<NAME>`, which reaches GlitchTip through the
workspace's own address.

## The deploy log and the history

Prod has a state folder, `/srv/<product>/prod/state/`:

- `deploy.log`: every line every deploy printed, each with its time in UTC.
- `history.jsonl`: one line per deploy that answered: when (`at`), the `commit`, the `previous` commit and whether
  it was a push `back`.
- `deployed`: the commit that runs now.

## Old images

After a deploy succeeds, the server keeps the product's images of the last `[deploy].keep_images` commits prod ran,
and every image a container still uses. It removes the product's other commit-tagged images.
Other images, and tags that are not commits, are never touched. A weekly clean-up removes old build cache and
dangling images ([server.md](server.md)).
