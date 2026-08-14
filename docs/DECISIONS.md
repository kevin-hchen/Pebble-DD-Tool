# Decisions — see `CLAUDE.md`

This file was a hand-copied snapshot of `CLAUDE.md`, which was gitignored. That
arrangement failed in the way two-copies-of-one-document always fails: the two
drifted, in **both** directions — 271 lines existed only in `CLAUDE.md` and 133
only here — and the copy a stranger got from a fresh clone was the stale one.

The decision record now lives in exactly one place:

> **[`CLAUDE.md`](../CLAUDE.md)** — in the repository root, tracked, and the file
> every `see CLAUDE.md` comment in the source already points at.

`CLAUDE.md` was chosen as the survivor rather than this file for three reasons,
recorded so the choice is not silently reversed:

1. **The code already points there.** Ten-plus modules carry `see CLAUDE.md` in
   their comments — `agents.py`, `markers.py`, `claims.py`, `diligence.py`,
   `providers.py`, `trials/store.py`, `trials/queries.py`, `fda/orangebook.py`,
   and two config files. Making this file the authority would have meant editing
   every one of those and breaking every pointer already written down elsewhere.
2. **It is the file that was actually maintained.** The 271-to-133 split is not
   symmetric drift; it is one live document and one occasional copy.
3. **Publishing it was already the recommended resolution.** `docs/RUNBOOK.md`
   listed the gitignore under "Known broken" with the note to decide
   deliberately: publish it, or hand it over out of band. This is that decision.

Nothing was lost in the merge: the device-parity decisions recorded here on
13 August 2026 were appended to `CLAUDE.md` before this file was replaced.

This stub is kept rather than deleted so that anything already linking to
`docs/DECISIONS.md` still arrives somewhere useful.
