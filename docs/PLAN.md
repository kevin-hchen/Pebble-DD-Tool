# The five-phase plan

Written down because it kept being asked. Phase 1 is complete and shipped;
phases 2-5 are **deferred, not abandoned, and not forgotten** — the tool is
going onto a public website this week, and deployment takes priority over
generality.

That is the whole reason. None of phases 2-5 was dropped because it turned out
to be a bad idea, and none of them was blocked by something in the code. They
are deferred against a date.

Each phase below states what it is, why it exists, and what a reader who picks
it up will need to know. Whoever inherits this repo should be able to start
phase 2 from this page plus `CLAUDE.md`.

---

## Phase 1 — Query sets ✅ COMPLETE

**What:** replace "one condition string" with a reviewed, unioned query set per
indication, and prove the fetch reached the whole population.

**Status: done and verified.** `config/trial_queries.yaml` holds 74 families;
all 74 are ingested and graded COMPLETE — 241,298 trials across 374 queries,
every query fetched to its own registry-reported total, independently recounted
from the `trials` table rather than trusted from the status column.

Shipped alongside it, because the ingest could not be called finished without
them:

- **The ingest lifecycle** (schema v9). `begin_ingest` marks a family
  IN_PROGRESS before the first network call; only a verified count clears it.
  Every other completeness guard here fires on a *response*, and a killed
  process returns no response at all.
- **Retry with backoff** in `trials/client.py`. 41 HTTP 500s and 12 dropped
  connections were measured across three passes; each one downgraded an entire
  family.
- `agents.py`, `markers.py`, `ranking.py`, `coverage.py`, `phrasing.py` — each
  one the single implementation of something two modules had been doing
  differently.

See `CLAUDE.md` for the reasoning behind each; this page is the plan, not the
decision record.

---

## Phase 2 — Numeric and ordinal gates ⏸ DEFERRED

**Deferred: public deployment this week takes priority over generality.**

**What:** eligibility criteria stated as numbers and orderings, matched as
numbers rather than as text. ECOG performance status 0-1, ejection fraction
≥40%, eGFR ≥30, platelet count, age bands, prior line counts.

**Why it matters:** these are the most common hard gates in oncology and
cardiology eligibility, and the current tool cannot read any of them. A trial
requiring ECOG 0-1 and a patient with ECOG 2 is an exclusion the tool will
happily miss, because "ECOG" is matched — if at all — as a string.

**What the implementer needs to know:**

- This is `markers.py`'s problem shape, not a new subsystem: parse a signal
  from eligibility text, reduce it under a policy. Expect the same
  patient-side / census-side split as `biomarker.py` vs `biomarker_gating.py`,
  with the same rule — share the vocabulary and the grammar, keep the
  conflict-resolution *policy* separate and deliberate.
- The negation and test-requirement grammar in `markers.py` already exists and
  will need extending, not forking. "Must have been assessed for X" is not a
  statement of X, and the same trap applies to numbers.
- An ordinal is not a number. ECOG 0-1 is a set, not a range to compare
  numerically against every scale; a schema that stores "≥" and "≤" without
  storing which SCALE will silently compare ECOG against eGFR.
- Units are the obvious hazard and are not the interesting one. The interesting
  one is that a criterion can be stated in a table, a footnote, or prose, and
  a missing gate must degrade to NOT MENTIONED, never to "passes".

---

## Phase 3 — Prior-therapy and population-band gates ⏸ DEFERRED

**Deferred: public deployment this week takes priority over generality.**

**What:** "progressed on or after at least two prior lines", "treatment-naive",
"no prior anti-PD-1", "no prior anthracycline exposure" — plus the population
bands trials use to slice a disease: line of therapy, resectability, metastatic
vs adjuvant, treatment-refractory status.

**Why it matters:** this is the single largest reason a patient or an analyst
looks at a trial and finds it does not apply. It is also where the diligence
question "who is the competition actually enrolling" is answered — two Phase 3
trials in the same indication at different lines of therapy are not competitors
in any meaningful sense, and the tool currently cannot tell them apart.

**What the implementer needs to know:**

- Prior therapy is `agents.py`'s matcher pointed at a different question. The
  drug-name matching is solved; what is not solved is the temporal and
  quantitative frame around it ("at least two prior", "within 6 months of",
  "no prior exposure to").
- A prior-therapy exclusion naming a DRUG CLASS ("no prior checkpoint
  inhibitor") cannot be resolved by `config/agents.yaml` as it stands — that
  table maps names to the same molecule, not molecules to a class. Adding a
  class layer is a real vocabulary decision and belongs in YAML, reviewed, for
  the same reason the marker table does.
- Absence is the trap again, in its sharpest form: a trial that does not
  mention prior therapy is not a trial that accepts any. Expect the
  not-found-is-not-permission rule to need stating explicitly in this phase.

---

## Phase 4 — Marker vocabulary expansion ⏸ DEFERRED

**Deferred: public deployment this week takes priority over generality.**

**What:** grow `config/markers.yaml` beyond the seven curated markers (MSS,
MSI-H, RAS, BRAF, HER2, KRAS G12C, KRAS G12D) to cover the biomarkers the other
73 indications gate on — EGFR/ALK/ROS1/KRAS in lung, ER/PR/HER2 in breast, IDH
and MGMT in glioma, FLT3/NPM1 in AML, and the fusion markers.

**Why it matters:** the trial store now holds 74 indications and the marker
vocabulary covers one of them. Every non-colorectal family is currently ingested
with a biomarker census that is structurally blank, and a blank census renders
as "not mentioned" — which is honest, but is a large stated gap rather than an
answer.

**What the implementer needs to know:**

- This is mostly a YAML and clinical-review task, not a code task. That is by
  design (`markers.py` owns the grammar, `config/markers.yaml` owns the
  vocabulary) and it is the reason this phase is cheap to resume.
- **It requires a re-ingest**, unlike the drug alias table. Marker gating is
  baked in at ingest into `biomarker_gating`/`biomarker_basis` token columns, so
  a vocabulary edit does not take effect against the database on disk. That is a
  deliberate asymmetry with `config/agents.yaml` and it is documented in
  `CLAUDE.md`; at 241,298 records the re-ingest is now the expensive part of
  this phase, so batch the vocabulary work rather than shipping markers one at
  a time.
- `curated: true` is load-bearing. An uncurated marker can only ever return
  UNCLEAR or NOT MENTIONED, never a confident verdict, and that must stay true
  as the table grows.
- Only MSS/MSI-H currently has an `opposite`. Check whether any newly added
  marker genuinely needs a paired opposite before adding one — most do not,
  because negation handles "wild-type" directly.

---

## Phase 5 — Coverage matrix ⏸ DEFERRED

**Deferred: public deployment this week takes priority over generality.**

**What:** one surface answering "what does this tool actually know, and how
well?" across every indication and every store — trials, drugsFDA, devices,
FAERS, Orange Book, Purple Book, literature — with each cell traced to a stored
count and a fetch date.

**Why it matters:** every individual coverage statement in this tool is honest
about its own scope, and there is still no way to ask the question across
scopes. An analyst cannot currently find out that colorectal has a curated
marker table and NSCLC does not, or that the literature corpus covers three
indications out of 74, without reading the config files. The tool's most
valuable property is that it states what it does not know; phase 5 is that
property applied to itself.

**What the implementer needs to know:**

- `coverage.py` already owns the one-render-function rule and should own this
  too. The matrix is an aggregation of statements that already exist, not a new
  source of truth — if a cell needs a number that no store records, the fix is
  in that store, not in the matrix.
- The three-state rule is the whole design: never searched / searched and found
  nothing / searched and found N. A matrix that renders an unsearched cell as 0
  would be the single most misleading screen in the tool, and it is the obvious
  way to build it by accident.
- `store.ingest_states()` and `query_coverage` already give the trials row of
  this matrix, including per-query retry counts and fetch dates. The FDA
  `catalog` table is the equivalent for devices. Those two are the model.

---

## Deferred, but not lost

If the public deployment lands and someone asks "what next", the honest ranking
is:

1. **Phase 4** first — cheapest, mostly YAML, and it unblocks the 73
   indications currently ingested with a blank marker census. Note the
   re-ingest cost before starting.
2. **Phase 2** next — the most common hard gate in real eligibility text, and it
   reuses `markers.py`'s existing shape.
3. **Phase 3** after that — the highest analytical value, the most parsing risk.
4. **Phase 5** last — it aggregates the other four, and it is worth most once
   there is more to aggregate.
