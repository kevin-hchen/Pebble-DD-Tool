# Criteria segmentation ground truth — the unit definition, fixed BEFORE the draw

Written first, and committed before a single record was sampled, for the same
reason `diagnostic_ground_truth.README.md` was: a label vocabulary chosen after
reading the sample is fitted to the sample. Nothing below was decided after
seeing a record, with one stated exception — the four separator classes come
from `scripts/enum_styles.py` run over the corpus, which is a frequency
measurement over the whole store and not a look at the units that will be drawn.

## What one unit is

There is no purpose-free answer to "what is one criterion". *"Patients with X
and Y"* is one criterion or two depending on why you are asking. So the unit is
defined by what the only consumer needs:

> **A unit is a span that carries exactly one polarity.** Two clauses of
> opposite polarity — one admitting, one excluding — must never share a unit.

That is the whole property `markers.iter_criteria` exists to provide, and the
only one anything downstream relies on. `markers._context` assigns a polarity to
a *unit* and `markers.collect_signals` then classifies every marker match inside
it against that one polarity. A unit spanning two polarities therefore hands one
of the two clauses the wrong direction — necessarily, not occasionally.

Defining it this way also makes correctness checkable **without marker
semantics**. That is deliberate. RATIONALE §23 records an attempt to fix a
related defect in direction vocabulary, which failed because the words that
signal direction and the words the marker names are built from are the same
words (MSI-**H**igh, MSS = microsatellite **stable**, **p**MMR = **proficient**).
Segmentation is graded on polarity spans alone, so that trap is not reachable
from here.

## Polarity

Assigned to a **clause read alone**, by its effect on eligibility — never
inherited from the section heading it sits under. The heading is exactly what
the code gets wrong, so grading against it would grade the defect as correct.

| value | the clause, read alone, states |
|---|---|
| `ADMITTING` | a condition that must hold, or a state that is permitted, for a participant to enrol |
| `EXCLUDING` | a condition that disqualifies a participant |
| `NEITHER` | no eligibility direction of its own — a definition, a heading fragment, a cross-reference, a note, or a mandate to *perform a test* without stating a result |

A unit is `MIXED_POLARITY` iff it contains at least one `ADMITTING` **and** at
least one `EXCLUDING` clause. Otherwise `SINGLE_POLARITY`.

`NEITHER` is load-bearing in the same way `NOT_MENTIONED` is: a clause with no
direction of its own is not a clause with the section's direction. It is the
label an orphaned fragment gets, and the over-split bar below is defined in
terms of it.

This is symmetric. An admitting carve-out inside an exclusion block — *"Prior
radiation is allowed but must have been completed ≥ 4 weeks prior; patients with
prior radiation to the liver cannot enrol"* — is a polarity mix exactly as much
as an excluding clause inside an inclusion block. Only the second direction has
been observed causing harm so far; the definition does not privilege it.

## The four separator classes

What sits **between** the two clauses of a mixed unit. Measured over the whole
store (241,254 trials, 3,946,369 units yielded by `iter_criteria`); the counts
are marker-bearing mixed-polarity units, the population that can actually flip a
verdict.

| class | n | reachable by a splitting rule? |
|---|---|---|
| `SENTENCE` | 60 | yes — plain sentence boundary, no marker of any kind |
| `SEMICOLON` | 43 | yes |
| `ENUMERATION` | 12 | yes — roman, arabic, lettered, parenthesised, bulleted or dash-led, appearing *inside* a unit |
| `NONE` | 22 | **no** |
| | **137** | |

`NONE` is stated here, before any work, because it is the ceiling. Two clauses
of opposite polarity inside one sentence with nothing between them cannot be
separated by any splitting rule. Reaching them needs clause-level polarity,
which is a different mechanism and a different piece of work. **The honest
ceiling for splitting is 115 of 137, about 84%**, and one `NONE` case is pinned
in this fixture as a residual precisely so it cannot quietly start reading as
handled.

## The bar is asymmetric, and over-splitting is NOT uniformly harmless

Under-splitting is the harm this work exists to remove: an excluding clause
absorbed into an inclusion block takes inclusion polarity, and a wrong direction
sends a reader to a trial that excludes their patient.

Over-splitting was initially treated as noise, on the reasoning that a fragment
lands in `NOT_ASSESSABLE` or `NOT_MENTIONED` — silence rather than a wrong
answer. **That holds for enumeration splitting and does not hold for sentence
splitting.** An orphaned clause loses its subject — *"This must be centrally
confirmed"*, *"These may be retrospective"* — and a fragment that names a marker
but carries no direction of its own will take polarity from its section. An
orphan in an inclusion section can therefore produce `REQUIRED` for a marker the
source never required. That is a manufactured direction, which is the same harm
under a different cause.

So the bar splits in two, and the two are measured separately because a single
"over-split rate" hides the one that matters:

| outcome of an over-split | bar |
|---|---|
| produces silence (`NOT_MENTIONED` / `NOT_ASSESSABLE`) | **reported, not gated** — expected, tolerable |
| produces a verdict the source does not state | **hard gate, zero** |

## Reading rule

Every row is read from the **complete eligibility criteria text of the complete
registry record**, not from an excerpt, and `source_span` on every row says so.

This is not a formality. The 28-trial `NOT_ASSESSABLE` hand-read
(`not_assessable_handread.json`) was performed against a dossier printing the
first 170 characters of each matching sentence, and two rows — NCT05700669 and
NCT06257758 — were labelled from an excerpt that stopped just before the clause
that decided them. Both blocks end *"Participants with HER2 positive disease are
not eligible for enrollment"*. **A truncated span is exactly what hides a
trailing exclusion clause**, which is the defect under investigation here, so
the failure mode of the method and the failure mode of the code are the same
failure mode. See `docs/DECISIONS.md`, "A hand-read is performed against the
complete record", and `tests/test_handread_provenance.py`.

`unit` is recorded **verbatim and complete** — never elided, never truncated —
so a later reader regrades from the fixture without going back to the store.

## What is recorded per row

- `nct_id`, `set_key` — provenance
- `unit` — the complete span `iter_criteria` yields today, verbatim
- `section` — what `iter_criteria` tagged it (`inclusion` / `exclusion` / `unknown`)
- `separator_class` — `SENTENCE` | `SEMICOLON` | `ENUMERATION` | `NONE`, and
  `NOT_MIXED` for the over-split strata
- `stratum` — `MIXED` (the defect population) or `OVERSPLIT_RISK` (units the
  splitting rule will touch without needing to)
- `verdict` — `MIXED_POLARITY` | `SINGLE_POLARITY`
- `clauses` — the hand segmentation: each `{text, polarity}`
- `markers_named` — curated markers the unit names
- `expected_marker_verdicts` — what the **record** supports for each, which is
  the acceptance target and is not the same as what the code returns today
- `reason` — why, in one sentence
- `source_span` — the reading rule, per row

## Why the over-split strata are in the same fixture

6.11% of units span more than one sentence and 112,370 carry an intra-unit
semicolon, against 137 marker-bearing mixed-polarity units in the whole store.
The splitting rule will therefore touch three orders of magnitude more units
than it fixes. A ground truth containing only the 137 would measure the fix and
be blind to the damage. `OVERSPLIT_RISK` rows are drawn from units that are
**not** polarity-mixed but do carry a separator the rule will act on, and they
are the population the second bar is graded against.
