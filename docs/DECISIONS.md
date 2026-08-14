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

---

## Memo length: constraint released 13 August 2026, summary-first not built

`docs/SCOPE.md` carried "two to three pages maximum" as a hard constraint. The
owner released it. Recorded here as well as there because a released constraint
is a decision, and the reason it was released is the useful part.

Measured before the decision, on real memos: **7 pages when nothing is found**,
**11 for a well-evidenced device**, **24 for a well-evidenced drug**. The 7-page
floor is the number that decided it — a memo that finds *nothing at all* cannot
currently be shorter, so 2-3 pages was unreachable by any amount of editing.

Composition of a well-evidenced memo, by character count: evidence blocks 46.3%,
source lists 26.4%, headers and coverage 21.0%. Length is therefore a **renderer
and question-count** matter, not a prose one. Trimming the model's paragraphs —
the obvious first move — reaches at most the remaining ~6%.

**Summary-first remains available and is NOT being built now.** A standalone 2-3
page front section, with the full evidence behind it, would satisfy the original
intent without shortening what the memo actually holds: the front section is what
a partner reads, the back is what an analyst checks a claim against. It is a
small renderer change — the memo already has every piece it needs, since
`coverage()` computes the counts and `render_lines()` already owns the one place
each block becomes text. What it needs is an owner decision about what belongs in
the front section, which is an editorial call and not a code one.

If it is built later, the thing to preserve is that the front section must not
become a *second* place where evidence is rendered. `coverage.render_lines` is
one function precisely because three renderers drifted before; a summary that
re-derives its numbers instead of reusing them would reintroduce that.
