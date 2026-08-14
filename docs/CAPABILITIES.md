# What this tool can and cannot do

Measured, not asserted. Every number here comes from a stated measurement with
its denominator; where something is unmeasured this file says so rather than
leaving the gap to be inferred.

---

## Diagnostic evidence grading

`medrag/diagnostic_grade.py` places diagnostic and prognostic studies on a
hierarchy built for the diagnostic question — Oxford CEBM levels for diagnosis,
with QUADAS-2 supplying the design facts and STARD supplying
stated-versus-absent. It sits BESIDE the therapeutic grader in
`evidence_grade.py`, which is unchanged.

### The number

**On a confirmatory set of 40 studies, drawn fresh and hand-labelled blind
before the grader was run, and graded once:**

```
misroutes    4 / 40  = 10.0%     pre-registered bar: <= 10%      MET
coverage    35 / 40  = 87.5%     guard: >= 85%                   MET
declines     5 / 40  = 12.5%     guard: <= 6                     MET
ordering inversions   0          hard gate: 0                    MET
```

A **misroute** is a study sent to the wrong hierarchy — the failure that gives a
reader a confident tier on a scale that does not apply. A **decline**
(`CANNOT_TELL`) is a study the grader refuses to place; it costs coverage and
harms nobody, which is why coverage is reported beside the misroute rate every
time. Without it, a grader could reach a perfect misroute rate by declining
everything.

**An ordering inversion** — ranking a two-gate case-control above a consecutive
cohort, or a non-consecutive above a consecutive — is the hard gate, because it
tells a reader the evidence is stronger than it is. Zero on the confirmatory
set.

### What is NOT measured

**Tier assignment is effectively unmeasured.** The confirmatory draw contained
only **9** studies the hierarchy applies to, of which 3 were graded to the exact
tier and 4 were comparable pairs. Nine studies across six tiers cannot support
an accuracy figure, and no tier-level claim is made here. The routing figure
above is the measured one.

**No case-control or prognostic-model study appeared in the confirmatory draw**,
so the tier that motivated the whole change — two-gate case-control, which the
therapeutic map put at 5 of 8 beside case reports — was not exercised at all.
Its ordering is pinned by unit tests and by the development set; it is not
confirmed here.

### What it replaces, so the figure has a baseline

Graded on the therapeutic hierarchy, 44 of 86 diagnostic-accuracy studies in the
audit corpus came back `Unclassified` and the rest clustered at tier 4 of 8. A
PubMed `Validation Study` type maps to `cohort` — tier 4 — regardless of the
study's actual design.

### Provenance

The development set (110 studies) is at
`tests/fixtures/diagnostic_ground_truth.json`, the confirmatory set (40) at
`tests/fixtures/diagnostic_confirmatory.json`. Both are content-hashed and
frozen; both carry per-study reasoning. The development figures are recorded in
`docs/DECISIONS.md` and deliberately do not appear here — the grader was
iterated against those labels, so they measure fit, not performance.

### Known label-quality notes, on the confirmatory set

Reported rather than corrected. The labels were frozen before grading and are
never edited, because an edited-once fixture has no reconstructible provenance.

* **PMID 39348147 and 38581254 are structurally the same design — a randomised
  trial of an invitation to screening — and I labelled them differently**
  (`NOT_DIAGNOSTIC` and `DIAGNOSTIC_RCT`). That is an inconsistency in the blind
  read, not in the grader. It does not move the headline: 39348147 was
  DECLINED, and a decline is not a misroute under either label — 4/40 both ways.
* **PMID 36618287** carries no `Review` publication type despite reading as a
  narrative review, which is why the grader routed it diagnostic. A defensible
  disagreement rather than a clear grader error.
