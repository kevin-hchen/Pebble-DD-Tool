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

---

## Stage A: the audit's device bands are superseded (15 Aug 2026)

Every device number in the parity audit came through a name regex later measured
at ~21% recall with 55% of its positives being drugs or procedures. Re-measured
against `intervention_types`, which the registry states. Gate-type patterns are
byte-identical to the audit's, so the only thing that changed is the population.

**Modality is not a registry fact.** The registry states DEVICE,
DIAGNOSTIC_TEST, DRUG, PROCEDURE; it does not state imaging vs monitoring vs
implant. That split existed only in the regex, so it is gone from these numbers
and the registry's own vocabulary is the axis.

### Eligibility verdict rate (whole store, not a sample)

```
DEVICE            20,118    0.5%        DRUG        116,552    5.9%
DIAGNOSTIC_TEST    7,152    1.9%        PROCEDURE    13,517    1.3%
DEVICE+DRUG        2,388    3.9%        OTHER        57,425    1.1%
devices combined  27,270    0.9%        UNKNOWN      24,102    1.9%
```

The gap is **6.5x**, not the ~12-38x the audit's per-modality bands implied. Both
sides move: the audit's drug control (11.4%) was a regex-selected subset enriched
for oncology, and its device bands (0.3-5.3%) were ~45% real devices.

SUPERSEDED, do not re-quote: imaging 3.3%, monitoring 0.7%, implant 0.3%,
surgical 1.2%, IVD 5.3%, drug 11.4%.

### Gate-type prevalence and device specificity

Sample of 5,000 per class. Ratio is (DEVICE + DIAGNOSTIC_TEST) / DRUG.

```
gate type                              device%   drug%   ratio
device_compatibility_or_tolerance         7.2     1.7    4.15x
care_setting                              8.0     3.9    2.04x
procedural_indication_or_referral        11.2     6.4    1.76x
anatomical_or_imaging_finding             5.9     6.1    0.97x
procedural_history_or_existing_device     9.2    10.3    0.89x
clinical_rating_scale_or_staging         23.4    48.6    0.48x
numeric_physiologic_threshold            25.0    59.3    0.42x
temporal_window_from_event               18.6    58.1    0.32x
specimen_type_or_adequacy                 9.3    36.6    0.25x
biomarker (what the tool has today)       4.3    21.6    0.20x
```

### The finding that changes the framing

**The three most common gate types in device trials are MORE common in drug
trials.** Numeric thresholds, rating scales and temporal windows are 0.32-0.48x
device-specific. The audit reported them as the top device gates, which was true
and misleading: they are the top gates in *every* trial.

Two consequences worth acting on. Building numeric thresholds and ordinal scales
serves BOTH paths, and serves the drug path more — so it is not device work
being done under a device heading, it is shared work that happens to have been
found by looking at devices. And the audit's claimed 11-16x enrichment for care
setting and device compatibility was inflated 2-4x by the denominator; the real
figures are 2.04x and 4.15x.

Only ONE axis is close to device-exclusive: `device_compatibility_or_tolerance`
at 4.15x — MRI conditionality, implanted metal, contrast allergy, inability to
lie still, body habitus against scanner dimensions. A drug trial has no
equivalent of it.

### Pattern precision, spot-checked before choosing

Read 8 matched sentences per candidate. `device_compatibility` 8/8 genuine;
`procedural_indication` 8/8; `care_setting` ~6/8 — it matched "high-speed access
to the internet at home or work" and a participant's own workplace, confirming
the over-firing suspected in the audit. Its 8.0% is an upper bound.

### Modality is absent as an axis, and what it would take to have one

The registry states `DEVICE`, `DIAGNOSTIC_TEST`, `DRUG`, `PROCEDURE`. It does
NOT state imaging versus monitoring versus implant versus surgical. That split
existed only in the parity audit's name regex, and reporting it would be
inventing a number.

**Every "across five modalities" instruction is therefore unsatisfiable and is
replaced by: stratify by registry class** — DEVICE, DIAGNOSTIC_TEST,
DEVICE+DRUG, PROCEDURE, with DRUG as control. This applies to the gate-type
work and to the Asian-registry work equally.

Recovering a modality axis at all needs a curated device-name table, and
`config/agents.yaml` has **zero** device entries — it is a drug generic/brand/
code table. Until one exists, "how does this perform on imaging versus
implants" is a question this tool cannot answer honestly, and the right answer
to it is to say so rather than to re-derive the old audit's bands. Queued, not
built.

### Stage D is the largest coverage win in the store, and is scheduled second for parity, not for value

Recorded because a later reader seeing D after C will otherwise assume D
mattered less.

`numeric_physiologic_threshold` is the highest-prevalence gate type in the
entire store: 25.0% of device trials and 59.3% of drug trials. It is by a wide
margin the largest single increase available in how often the eligibility screen
answers anything at all, on both paths.

And the baseline is sparser than the device framing suggests. The drug verdict
rate is **5.9%**, not the 11.4% the audit reported — that figure was a
regex-selected, oncology-enriched subset. So the honest picture is not "devices
are broken and drugs work". It is that the screen returns a verdict on roughly
one drug trial in seventeen and one device trial in a hundred. **Both are
sparse**, and even the 5.9% is mostly one therapeutic area, because all seven
curated markers are oncology molecular markers.

Two different goals, which point at different builds and are not collapsed here:

  * *make the screen answer at all* -> numeric thresholds, first, by a distance
  * *make devices equal to drugs* -> device compatibility, which is Stage C

Stage C is first because the parity commitment in `docs/SCOPE.md` is the one the
owner has stated most often, and because device compatibility is the only
candidate axis with no drug counterpart — so it tests whether parity is
achievable rather than borrowing machinery that already works for drugs. That is
a reason of parity. It is NOT a judgement that D is worth less.

---

## A hand-read is performed against the complete record — recorded 27 August 2026

**The rule.** When a label is produced by reading a record, it is read from the
COMPLETE record — the whole eligibility block, the whole abstract — never from
an excerpt, a truncated span, or a dossier summary generated to make the reading
convenient. The fixture that stores the labels records WHICH it was, per row or
in a README beside it, so a future reader can tell whether this failure mode
applies to it.

**Why, with the case.** The 28-trial `NOT_ASSESSABLE` hand-read
(`tests/fixtures/not_assessable_handread.json`) was performed against a
generated dossier that printed the first 170 characters of each matching
sentence. Two rows — NCT05700669 and NCT06257758 — were labelled BELONGS on that
basis. Both criterion blocks end:

> "Participants with HER2 positive disease are not eligible for enrollment."

past the end of what was printed. Both records state a direction; both labels
were wrong, and wrong in the same way, because the same excerpt boundary hid the
same clause. The corrected split is 9 prose-sourced / 11 direction-swallowed /
8 belongs — 20 wrong of 28, not 18.

**What makes this worth a standing rule rather than a correction.** The defect
is in the METHOD, not in the reading. A hand-read is the control this project
falls back on when every automated gate is clean — it is what caught the Stage B
emission being wrong on most of the records it fired on. A control with a silent
truncation in it produces confident labels that are wrong in a correlated way,
and it then freezes them into a fixture that everything downstream is measured
against. That is worse than no control, because it looks like one.

**Applies to.** `not_assessable_handread.json` (28, per-row `source_span`),
`diagnostic_ground_truth.json` (110) and `diagnostic_confirmatory.json` (40),
whose span is stated in `diagnostic_ground_truth.README.md`: title, abstract and
PubMed publication types — the complete record available for a published study.
`tests/test_not_assessable_handread.py::test_every_row_records_the_span_it_was_read_from`
and `tests/test_handread_provenance.py` enforce it.
