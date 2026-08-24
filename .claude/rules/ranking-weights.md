---
paths:
  - "config/ranking.yaml"
  - "medrag/ranking.py"
---

# Two weights that must be argued for in the YAML, not drifted

Both are already pinned by tests, so the prose here is the reason a reweight has
to be deliberate rather than the enforcement. They are scoped to these two files
because a weight can only be changed here: no caller chooses its own weights,
and nothing outside this glob reads them.

Full per-signal justification: `config/ranking.yaml`'s own header, and
`docs/RATIONALE.md` §4 for the ground-truth measurement taken after the scheme
shipped.

## What stays in CLAUDE.md

That the score is deterministic and explainable with no model call anywhere in
the path (a caller in `diligence.py` could add an LLM re-rank); that ties break
on NCT ID and anything deciding a row's position must appear in `explain()`
(three renderers print that line); and that `phase` is stored post-conversion
(`"EARLY_PHASE1"` → `"EARLY_Phase 1"`), which bites anyone writing a phase value
into any config file, not just this one.

**`sponsor_class` is excluded ENTIRELY, not weighted low.** It answers who is
paying, not how urgently a row should be read, and this codebase already treats
non-industry evidence as no less trustworthy elsewhere (`disclosures.py`'s
independence axis). Folding it into an urgency score would bias the printed
sample toward industry sponsors for a reason that has nothing to do with
urgency. It stays visible on its own, in `by_sponsor_class`.
→ `test_shipped_config_never_scores_sponsor_class`

**`proximity` stays weighted BELOW phase and status**, and is the arguable
number in this file. Distance is a patient's hardest practical constraint, but
the match is an ungeocoded substring test where "same state" can mean a six-hour
drive. With no location given the signal is not evaluated at all rather than
scored as zero — "not applicable here" and "scored and found nothing" are
different statements.
→ `test_shipped_config_keeps_proximity_below_phase_and_status`

**Phase and status stay weighted above the secondary signals** — they are the
densest, most direct answers to "should this be read now". Enrolment and
randomisation are tied at a moderate weight; site count sits below enrolment
because it is largely the same underlying fact counted twice; recency is a
tiebreaker rather than a driver.
→ `test_shipped_config_weights_phase_and_status_above_the_secondary_signals`

A reweight that moves any of these has to change the test in the same commit,
which is the point: the argument goes in the YAML header where the next reader
finds it.
