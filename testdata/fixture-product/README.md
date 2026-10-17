# Notes

A tiny notes app the factory proves itself on. It has the standard shape every product takes: a goal written by a
person, standing QA scenarios, values for the standard checks, one script with the fixed verbs, and the settings
for the standard build and deploy.

- `app/` holds the notes themselves, the app's one door to the network, and the image the page ships in.
- `web/` holds the page that lists the notes.
- `tests/` holds the tests `scripts/dev test` runs.
- `qa/` holds the scenarios testers walk.
- `scripts/` holds the stack script.
- `factory.toml` describes how the app is built and deployed; `compose.yml` is its stack and
  `notes.env.template` its settings. The factory's tests read them; nothing builds or runs them.
