---
protects: app/notes.py web/app.js
needs: nothing
runs: in-turn
---
# Adding a note

A person relies on a note showing at once, at the top, exactly as they wrote it.

## Steps

1. Open the page.
   - Expect: the box to add a note sits above the list.
2. Write "Buy milk" and press Add.
   - Expect: "Buy milk" is the first note in the list.
3. Press Add with the box empty.
   - Expect: the page says a note needs some text, and the list is unchanged.

## What would be a bug

- A note that shows anywhere but first, or with its text changed.
