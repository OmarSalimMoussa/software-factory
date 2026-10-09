---
protects: app/notes.py
needs: seed three-notes
runs: in-turn
---
# Finding a note

A person relies on a search listing every note that holds the word, and only those.

## Steps

1. Open the page with the three saved notes loaded.
   - Expect: three notes are listed.
2. Search for "milk".
   - Expect: only "Buy milk" is listed.

## What would be a bug

- A note listed that does not hold the word, or one that holds it left out.
