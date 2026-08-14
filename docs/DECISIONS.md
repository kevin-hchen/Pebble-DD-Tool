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

---

## Two limits found while hand-reading the diagnostic ground truth (14 Aug 2026)

Recorded because they were discovered by reading 110 abstracts, and the next
person should not have to rediscover them the same way.

### 1. The frozen vocabulary conflates prognostic ASSOCIATION with prognostic PREDICTION

`PROGNOSTIC_MODEL` is defined as "development and/or validation of a prediction
model". PMID 36889038 — faecal haemoglobin concentration followed against a
national death register — is a prognostic **association cohort**: a marker
tested for association with a future outcome, with no model developed or
validated. It carries `PROGNOSTIC_MODEL` because that was the closest value in
a vocabulary fixed before the sample was drawn, and the mismatch is written into
its `reason` field.

**Not fixed, deliberately.** The vocabulary was fixed first precisely so it
could not be reshaped by what turned up, and editing it mid-read would have made
every label before that point incomparable with every label after.

**First item for a v2 vocabulary.** The split wanted is roughly
`PROGNOSTIC_ASSOCIATION` (a marker tested against a future outcome) versus
`PROGNOSTIC_MODEL` (a multivariable model developed, internally validated, or
externally validated) — the distinction PROBAST and the TRIPOD statement draw,
and the same distinction that separates "this is associated with outcome" from
"this predicts outcome for an individual". Two of 110 studies are affected, so
it changes nothing about the current measurement; it will matter as soon as
prognostic studies are a target rather than a residual.

### 2. A sampling convention was settled mid-read, and reasonable people could draw it elsewhere

CEBM separates a validating cohort (1b) from a non-consecutive study (3b) on
sampling. Most abstracts state neither "consecutive" nor "selected". The
convention applied uniformly from that point on:

- **prospective** study in a defined care population, sampling unstated ->
  `CONSECUTIVE_COHORT`
- **retrospective** record review, sampling unstated -> `NONCONSECUTIVE_COHORT`

The reasoning: retrospective selection from records is where verification bias
actually lives, and prospective enrolment in a care setting is the defining
feature of a clinical cohort. But this is a judgement, not a reading of the
text, and someone could reasonably require the word "consecutive" before
awarding 1b — which would move a number of studies down one tier.

**This is why `sampling` and `timing` are recorded per study in the fixture.**
Any disagreement between a grader and these labels can be traced to whether it
turns on this convention or on a genuine misreading of the design, and those are
different findings. Any measurement against this fixture must report that split
rather than a single disagreement count.

---

## The diagnostic ground-truth fixture is NEVER edited — including in a v2 pass

`tests/fixtures/diagnostic_ground_truth.json` is content-hashed and frozen. The
rule is stronger than "do not edit it while measuring": **it is never edited at
all.** A v2 vocabulary gets a NEW fixture, drawn and hand-read fresh under the
new vocabulary. An edited-once fixture is a fixture whose provenance nobody can
reconstruct — was this label from the original blind read, or added later once
someone had seen what a grader did with it?

### PMID 41028541 — a known label-quality note, and why it stays

*Clinical validation of an AI-based blood testing device for diagnosis and
prognosis of acute infection and sepsis.* The fixture labels it
`CONSECUTIVE_COHORT`. The grader returns `CANNOT_GRADE`, on the ground that the
abstract states neither sampling nor prospective/retrospective.

**The grader is probably right.** The label rested on "emergency-department
patients presenting with non-specific symptoms" being the intended-use
population, which is an inference about design from the setting — and the
reading rule recorded beside the fixture says the opposite: never inferred from
the journal, the topic, or what such studies usually do.

**It stays as it is, permanently.** The observation was made AFTER seeing grader
output, so it cannot cleanly re-enter this fixture at any point — not now, and
not in a v2 pass, because "we already know the grader disagrees here" is exactly
the knowledge a blind read must not have.

What this is instead: a case the v2 read should handle EXPLICITLY. A vocabulary
that keeps `CONSECUTIVE_COHORT` and `CANNOT_GRADE` apart needs to say what
happens when a clinical setting is stated and sampling is not — either the
setting counts as evidence of a consecutive series or it does not, and v2 should
decide that in the vocabulary rather than leaving it to each reader.

Recorded so the next person does not rediscover it, and does not "fix" it.

---

## The decision rule for the confirmatory draw, set before drawing

The development figure landed at exactly the bar with no margin, after iterating
against those labels. Optimisation pressure plus a fresh sample means the
confirmatory number will very likely be worse. That is expected. Deciding what
happens in advance is the only thing that makes the result mean anything.

**The confirmatory number is the published number, whatever it is. The
development figure never appears in `CAPABILITIES.md`.**

| confirmatory misroute rate | what happens |
|---|---|
| **<= 10%** | ship; publish the confirmatory figure |
| **10-20%** | ship anyway; publish the confirmatory figure WITH the pre-registered bar stated beside it, so the miss is on the record |
| **> 20%** | does not reach a memo. Fix the named pattern, then draw a NEW confirmatory set |

Shipping in the 10-20% band is deliberate, and the reasoning belongs on the
record rather than in a decision made later under pressure: what this replaces
is *actively wrong*. The therapeutic map sends a `Validation Study` to tier 4 of
8 and left 44 of 86 IVD studies unclassified or bottom-tier. A grader that
misroutes 15% is a large improvement on one that is systematically wrong, and
saying so plainly beats withholding it.

**Never re-grade the same 40.** A confirmatory set is confirmatory once. If the
grader is changed after seeing it, the next measurement needs a new draw.

**Zero ordering inversions remains a hard gate at every level.** An inversion on
the confirmatory set is a stop-and-report regardless of the misroute rate — it
is the failure that tells a reader the evidence is stronger than it is.

### Development-set figures, for the record only (14 Aug 2026, 110 studies)

Stated here so they are not mistaken later for the published number:

```
misroutes                6/110  = 5.5%   (bar <= 10%)
coverage (routed)      101/110  = 91.8%
declined CANNOT_TELL     9/110  =  8.2%
exact tier              35/52   = 67.3%
within one tier         33/33 comparable pairs
ordering inversions      0
```
