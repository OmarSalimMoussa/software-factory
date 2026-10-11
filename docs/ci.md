# CI

Every product runs the same two GitHub workflows. They live in the factory's `templates/ci/`, and the product copies
them into `.github/workflows/` and never edits them: the `deploy-shape` check refuses a copy that differs by one byte.
Everything that belongs to one product comes from its `factory.toml`.

| Workflow | Runs | Does |
| --- | --- | --- |
| `ci.yml` | every push, and by hand | checks every push; builds, smoke-tests and deploys staging from the default branch |
| `deploy-prod.yml` | when the warden starts it | deploys prod, then publishes the releases that changed |

## ci.yml

On every push, the **checks** job checks out the factory at the commit `.github/factory-ref` pins and runs the
factory's checks over the whole product. A newer push to the same branch cancels an older run's checks, and so the
rest of that run.

For a push to the default branch, or a run started by hand, these jobs follow, in order:

1. **plan** lists the images: every service in `compose.yml` with a `build:` section, plus `[build].extra_images`.
2. **build** builds and pushes each image as `<registry>/<name>-<image>:<short>`, where `<short>` is the commit's
   first 8 characters. Every image gets the build argument `[build].commit_arg` set to `<short>`. Layers are cached
   in the registry under the tag `buildcache`.
3. **build-derived** builds the images whose Dockerfile starts `FROM` another image of the same commit, once that
   one is pushed. They get `[build].tag_arg` too. The job is skipped when nothing derives.
4. **deploy-image** packs everything a deploy needs on the server into `<registry>/<name>-deploy:<short>`: the files
   and folders `[deploy].files` names, each workspace's after-deploy hooks, `factory.toml` and the plan. The server
   pulls this image and builds nothing.
5. **smoke** runs the smoke test below.
6. **deploy-staging** asks the server to deploy staging, over SSH as its `ci` user. The server accepts nothing but a
   deploy command, and pulls the images with the run's own registry token.

A deploy to staging and a deploy to prod never run at once on the server, and neither is cancelled half way. Each
has its own queue on GitHub, so a staging deploy never replaces a waiting prod deploy: in each queue, a newer waiting
deploy replaces an older one that has not started. A push deploys staging only while it is still the default
branch's newest commit, so an older run that reaches its deploy late never takes staging back.

### The smoke test

The smoke job starts this commit's images on the CI runner, as a scratch workspace named `smoke`, before anything is
deployed:

1. A scratch Postgres with an empty database, on the network the factory's shared services use.
2. An env file written from the product's template, as on a server, and linked next to `compose.yml`. Every error
   and trace address points at a port where nothing listens: the stack must start and answer without them.
3. Every service `compose.yml` builds is pulled and started; compose starts what they depend on.
4. Each of those services must keep running. One with a healthcheck must turn healthy. One named in `[smoke].expect`
   must answer each path with its status. A service that stops fails the test at once, with its last 50 log lines.
5. The service `[smoke].health` names must answer `[deploy].health` as this commit (see below).
6. The product's own check, `[smoke].check`, runs last, if the product has one.
7. Everything is removed again, whether the test passed or not.

Each wait gives up after `[smoke].timeout_seconds`. Requests come from a small probe container that shares the
service's network, so no product image needs `curl` or any other tool. The test runs only on a CI runner: it refuses
to start where a container named `factory-postgres` already runs, such as beside the shared services on a laptop.

The product's check runs from the top of the repo with these variables:

| Variable | Holds |
| --- | --- |
| `FACTORY_PROJECT` | The compose project, `<name>-smoke` |
| `FACTORY_WORKSPACE` | `smoke` |
| `FACTORY_COMMIT` | The 8-character commit under test |
| `FACTORY_ENV_FILE` | The env file the stack runs with |
| `FACTORY_COMPOSE` | The compose command for this stack, quoted for a shell; add a verb such as `exec` |
| `FACTORY_TIMEOUT_SECONDS` | `[smoke].timeout_seconds` |

### Pin staging to another branch

A push to the default branch moves staging to that commit. To try another branch, tag or commit on staging, start
`ci.yml` by hand (Actions, then Run workflow) with its name as `ref`. It is checked, built, smoke-tested and deployed,
and staging stays on it: later pushes to the default branch are still checked, built and smoke-tested, but staging
does not move. To lift the pin, start `ci.yml` by hand with the default branch.

## deploy-prod.yml

The warden starts it when the owner presses Push to prod: with the full commit staging runs, or, for a push back, the
commit prod ran before, and with `released`, the commit the last releases that finished ran at (`-` before any
did). It refuses anything but a full 40-character commit. It then deploys prod over SSH.

After the deploy, the **releases** job publishes each `[[release]]` that is ready:

- A release with `live = false` is skipped. Set it to `true` once the store has approved the first version by hand.
- A live release is published only when something in its `when_changed` paths changed since `released`, or when
  no releases finished before. The warden moves `released` only when the releases job succeeds, so a release that
  failed is tried again at the next push.
- The product's packaging hook runs as `<package> <version> <origin> <zip>`: the version is `1.0.<run number>` of
  `deploy-prod.yml`, the origin is `https://<prod domain>`, and the hook writes the zip. The hook never sees the
  store's credentials.
- The zip goes to the Chrome Web Store, which reviews it before people get it.

Each release prints one line: published, skipped and why, or failed and why. A failed release fails the run, so the
warden reports the push as failed even though prod is live; the run's page says which job failed.

## What the product must answer

`[deploy].health` on every workspace answers status 200 with a JSON object:

- `workspace`: the workspace the stack runs as (`staging`, `prod`, or `smoke` in the smoke test). The product reads
  it from its env file: `[env.set]` can write `{workspace}` into any key.
- `build_commit`: the 8-character commit the image was built from, which the build argument `[build].commit_arg`
  carries.
- `busy`: a whole number, the work in flight. A deploy waits while it is above 0, up to `[deploy].drain_minutes`.

The smoke test checks all three, so a product that answers wrongly never reaches staging.

## Secrets

Set these in the product repo's Actions secrets. GitHub gives every run its own `GITHUB_TOKEN`, which pushes the
images; so `[product].registry` must be a `ghcr.io` path the repo may write to.

| Secret | Holds | Read by |
| --- | --- | --- |
| `FACTORY_READ_KEY` | The private half of a read-only deploy key on the factory's repo | all jobs that run the factory's code |
| `SERVER_DEPLOY_KEY` | The private key of the server's `ci` user, from the server set-up | the staging and prod deploys |
| `EXTENSION_STORE_EXTENSION_ID` | The extension's id in the Chrome Web Store | the releases |
| `EXTENSION_STORE_PUBLISHER_ID` | The store publisher's id | the releases |
| `EXTENSION_STORE_CLIENT_ID` | The Google API client's id | the releases |
| `EXTENSION_STORE_CLIENT_SECRET` | The Google API client's secret | the releases |
| `EXTENSION_STORE_REFRESH_TOKEN` | A refresh token for that client | the releases |

The five store secrets are needed only once a release is live; then a missing one fails the releases job, naming it,
before anything is packaged. A missing `SERVER_DEPLOY_KEY` fails its job the same way.
