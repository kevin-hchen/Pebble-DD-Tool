# Diagnostic-study label vocabulary — fixed BEFORE the sample was drawn

Written first, from published frameworks, so the labels are not shaped by what
happens to be in the sample. Nothing below was chosen after reading a study.

## Sources

- **Oxford CEBM Levels of Evidence for Diagnosis** (2011 and the 2009 diagnosis
  table) — the tier ordering. It is the only widely used hierarchy built for
  diagnostic accuracy rather than adapted from therapy.
- **QUADAS-2** (Whiting et al., Ann Intern Med 2011) — the design facts that
  decide the tier. Its four domains are patient selection, index test,
  reference standard, and flow & timing; the two that separate designs rather
  than merely grade their conduct are **sampling** (consecutive/random vs
  two-gate case-control) and **verification** (reference standard applied to
  all, vs partial/differential).
- **STARD 2015** (Bossuyt et al.) — the reporting items that tell me, from an
  abstract alone, whether a design fact was *stated* or merely *absent*.

## The label

Each study gets ONE `design` value. The values are ordered; the order is the
CEBM diagnostic hierarchy, not the therapeutic one.

| value | what it is | CEBM analogue |
|---|---|---|
| `SR_META_DTA` | systematic review / meta-analysis **of diagnostic accuracy studies** | 1a |
| `CONSECUTIVE_COHORT` | single-gate: a clinically relevant series, consecutive or random, reference standard applied to all | 1b |
| `NONCONSECUTIVE_COHORT` | single-gate but selected, convenience-sampled, or with partial/differential verification | 3b |
| `CASE_CONTROL_TWO_GATE` | known cases recruited separately from known controls | 4 |
| `DIAGNOSTIC_RCT` | randomised comparison of **test-and-treat strategies**, outcome is patient outcome not accuracy | (own axis) |
| `PROGNOSTIC_MODEL` | development and/or validation of a prediction model | (own axis) |

Plus two states that are **not tiers**:

| value | meaning |
|---|---|
| `NOT_DIAGNOSTIC` | not a diagnostic/prognostic accuracy study at all — therapy, mechanism, epidemiology, survey |
| `CANNOT_GRADE` | it **is** a diagnostic study and the record does not state a design that can be placed |

`CANNOT_GRADE` is the load-bearing one. A study whose design the abstract does
not state is not a weak study; it is an ungraded one. Collapsing it into the
bottom tier is the same error as reading "not mentioned" as "not eligible".

## Why `CASE_CONTROL_TWO_GATE` ranks low here, and why that is not the old bug

A two-gate design — sick people from one place, healthy people from another —
inflates accuracy, because the two groups differ in more than the target
condition. That is a real, measured effect (Lijmer et al., JAMA 1999: two-gate
designs overestimate diagnostic odds ratios roughly threefold). Ranking it below
a consecutive series is correct.

The defect being fixed is NOT that it ranks low. It is that it ranked low **on a
scale built for therapy**, at tier 5 of 8, adjacent to case reports — when for
early diagnostic validation it is a recognised and expected design. Here it is
tier 4 of 6 on a scale where the tiers above it are the designs it is genuinely
weaker than, and nothing below it except designs that are genuinely worse.

## Supporting facts recorded per study

Recorded because they are what the tier is derived FROM, so a disagreement can
be traced to a fact rather than to a judgement:

- `sampling`: `consecutive` | `random` | `selected` | `two_gate` | `not_stated`
- `reference_standard`: `stated` | `not_stated` | `not_applicable`
- `timing`: `prospective` | `retrospective` | `not_stated`

## Reading rule

I label from **title + abstract + PubMed publication types only** — exactly what
a corpus record holds, and therefore exactly what any grader can see. Reading the
full text would produce labels no grader could reproduce, which would make the
agreement measurement meaningless in the direction that matters.

Where the abstract states a fact, I take it. Where it does not, `not_stated` —
never inferred from the journal, the topic, or what such studies usually do.
