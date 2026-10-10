# Notes

A tiny notes app the factory proves itself on. It has the standard shape every product takes: a goal written by a
person, standing QA scenarios, values for the standard checks, and one script with the fixed verbs.

- `app/` holds the notes themselves and the app's one door to the network.
- `web/` holds the page that lists the notes.
- `tests/` holds the tests `scripts/dev test` runs.
- `qa/` holds the scenarios testers walk.
- `scripts/` holds the stack script and the steps CI calls.
- `.github/` holds the factory's CI, copied unchanged, and the factory commit it checks with.
