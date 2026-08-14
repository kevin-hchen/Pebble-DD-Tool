# CLAUDE.md — working notes for Claude Code

Read this before changing anything. It records decisions that look wrong until
you know why they were made, and the conventions the test suite depends on.

## What this is

A diligence tool for biomedical assets, used at a healthcare VC. Given an asset
and an indication it runs a fixed question set against two evidence stores —
published literature (PubMed) and the clinical trial registry
(ClinicalTrials.gov) — and produces a Markdown + PDF memo where every claim
carries a PMID or NCT identifier.

It is not a chatbot. The fixed question set is the point: the same questions in
the same order against every asset is what makes two memos comparable, which is
what makes the output usable in an investment memo.

Primary user: analysts and interns who will not open a terminal. The Streamlit
app is a launcher that produces a PDF, not a place to read findings.

## Decisions that must not be quietly reversed

**Trial records are NOT in the vector index.** Phase and status are filters, not
semantics. Embedding "Phase 3, TERMINATED" as prose and hoping cosine similarity
recovers it destroys the precision the registry exists for. Trials live in SQLite
with indexes; literature lives in FAISS; `router.py` decides which answers a
question. If you find yourself about to embed trial records, this is why they
aren't.

**The router keeps a rule-based fallback.** It runs whenever no model is
available, and the LLM path falls back to it on bad output rather than to BOTH.
A router that silently degrades to always-BOTH looks like it is working while
doubling cost and diluting every answer.

**The negative-evidence pass has two halves that do different jobs.** The
deterministic half (`store.stopped_trials`) is pure SQL and cannot hallucinate.
The model half is explicitly permitted to return an empty findings list — a
forced-contradiction prompt manufactures one, and an invented contradiction in a
diligence memo is worse than silence. Do not "improve" the prompt by removing
that permission.

**Stopped-trial lookup ORs intervention and indication, never ANDs them.** A
trial of the same compound terminated in a *different* indication is among the
most valuable things a diligence pass can surface. ANDing hid it. That was a bug
with a regression test (`test_stopped_trial_in_other_indication_is_not_hidden`).

**The two arms have separate budgets and separate denominators, because they
answer different questions.** The intervention arm asks "has this compound
failed anywhere, in any disease"; the indication arm asks "what has failed in
this disease". The first is the higher-value answer and the one nothing else in
the tool can produce. They used to share one budget of 25 after a merged sort,
which is a fair fight only if the pools are comparable — they are not: measured
on the live colorectal store the indication arm holds 1,336 stopped trials
against an intervention arm of 2-93. See "the stopped sweep" below for the
sizing, which was set by measurement after the first attempt at it was wrong.

**"Not assessed" and "nothing found" are kept distinct everywhere.**
`ValidationReport.assessed` exists so a section nobody checked cannot report a
clean pass; `NegativeEvidence.searched` does the same for the contradiction
hunt. Reporting an unchecked section as passing is a false negative dressed as a
pass. This has already been a real bug once — the memo claimed 10/10 sections
passing validation when the honest number was zero.

**Claim verification scores two orthogonal axes, never one.** In `claims.py`
every claim gets a `support` value (SUPPORTED / PARTIALLY SUPPORTED /
CONTRADICTED / NOT FOUND / NOT VERIFIABLE / UNVERIFIED) *and* an `independence`
value (INDEPENDENT / MIXED n-of-m / COMPANY ONLY / N/A). They do not trade off:
a claim can be PARTIALLY SUPPORTED and INDEPENDENT, or SUPPORTED and COMPANY
ONLY. The earlier design folded independence into a single `SUPPORTED - COMPANY
SOURCE` support value; that hid an independent partial behind a scary label and a
company-only support behind a reassuring one. Both axes are first-class columns
in the table, Markdown, and PDF. Do not re-merge them.

**Independence values: COMPANY-LINKED / NO DISCLOSURE / INDEPENDENT / MIXED /
N/A — and absence of a disclosure is never independence.** `_apply_independence`
classifies each *supporting* citation via `source_linkage`: COMPANY-LINKED if a
disclosure ties it to the manufacturer, INDEPENDENT only on *positive* evidence
(a trial sponsor that is not the manufacturer, or a paper with a named
non-industry funder or an explicit no-conflict statement), and otherwise NO
DISCLOSURE — the honest default. This is the not-found-is-not-contradicted rule
applied to the independence axis: nothing found either way must not read as a
clean INDEPENDENT pass, which in practice makes INDEPENDENT rare. A mix reports
its counts (MIXED 1 company-linked, 1 no disclosure). The model judges only
support; independence is computed in code.

**The company link is judged from a document-level disclosure, not the cited
chunk.** A paper's "Funded by X" line lives in its Conclusions or COI statement
while the cited result lives in Results — a different chunk — so judging linkage
from the cited chunk alone read industry-funded pivotal studies as independent.
`disclosures.py` derives a funder/affiliation/COI signal once per document at
ingest (from `AffiliationInfo`, `GrantList`, `CoiStatement`, and a scan of the
whole abstract) and `chunking.py` stamps it onto *every* chunk, exactly as
evidence grading does. Adding those chunk fields bumped the index schema
(`vectorstore.INDEX_SCHEMA`); an index built before the change is refused on load
with a rebuild instruction, the same as an embedder mismatch.

**Claim verification keeps NOT FOUND and CONTRADICTED strictly apart.** An empty
retrieval is NOT FOUND *deterministically* — the model is never consulted, so it
can never turn "nothing retrieved" into "evidence against". `UNVERIFIED` (evidence
retrieved but no model judged it) is a third distinct state, for the same reason
`ValidationReport.assessed` exists.

**NOT VERIFIABLE is decided at extraction, not verification, and never silently
discarded.** "Best-in-class accuracy" and "clinically proven" have no checkable
assertion; left alone they all come back NOT FOUND and drown the claims that
matter. `extract_claims` tags each claim's verifiability with a reason so the
analyst can rewrite or drop it during the edit step; `triage_claims` does the
same for a pasted/file claims list. Unverifiable claims are recorded as NOT
VERIFIABLE, because "the deck makes four claims that cannot be checked" is itself
a finding.

**The numeric downgrade is a deterministic overlay, not a model output.** The
model only ever returns supported / partial / contradicted / not-found; whether a
claimed figure is grounded (via `validation.figure_grounded`) is decided in code
afterward, downgrading SUPPORTED to PARTIALLY SUPPORTED with both figures shown.

**Biomarker matching never silently drops a trial, and always shows its
evidence.** `biomarker.py` screens a trial's eligibility text from the patient's
side (they HAVE the biomarker). It has five states now, not three:
ELIGIBLE (a positive variant named directly — MSS / microsatellite stable /
pMMR / proficient mismatch repair / non-MSI-H), ELIGIBLE BY EXCLUSION (the
*opposite* biomarker is excluded — "Exclusion Criteria: MSI-H or dMMR" — naming
theirs only indirectly), EXCLUDED (the trial requires the opposite, or excludes
theirs), UNCLEAR (the source text genuinely contradicts itself), and NOT
MENTIONED. Every candidate state (ELIGIBLE, ELIGIBLE BY EXCLUSION, UNCLEAR) stays
in the landscape, flagged, with its criterion sentence — a missed trial is worse
than an uncertain one for a patient looking for an option, and a filtered list
with no shown evidence cannot be checked. EXCLUDED and NOT MENTIONED are
*counted*, never hidden without a number.

**ELIGIBLE BY EXCLUSION exists because "excludes the opposite" used to have no
representation at all.** Before this state existed, a trial that names only
MSI-H — by excluding it — was folded into UNCLEAR, indistinguishable from a
genuinely self-contradictory trial. That is how two Phase 3 trials central to
the MSS mCRC population, STELLAR-303 (NCT05425940) and HARMONi-GI3
(NCT07228832), used to vanish from an MSS patient's search: an oncologist reads
"excludes MSI-H" as strong, confident evidence of MSS eligibility, not as
uncertainty. UNCLEAR is now reserved for an actual contradiction in the source
text (both an eligible-leaning and an excluded-leaning signal present, or one
sentence naming both markers — "MSI-H or MSS accepted").

**The vocabulary and negation grammar are shared, in `markers.py`; the
conflict-resolution POLICY is deliberately not.** `biomarker.py` (patient-side)
and `biomarker_gating.py` (trial-side census) used to carry separate copies of
the marker regex table, and `biomarker_gating.py` had a negation check while
`biomarker.py` had none at all — so the two could, and did, disagree about the
same trial. `markers.py` is now the one place that owns the marker table
(loaded from `config/markers.yaml`), section/sentence splitting, the negation
grammar, and `collect_signals()`, which scans text for a marker and its paired
opposite (MSS/MSI-H is the only pair with an established `opposite` — see
`config/markers.yaml` for why RAS/BRAF/HER2/KRAS do not need one: a trial
requiring "RAS wild-type" excludes RAS directly via negation, it does not need
a paired "RAS_WT" key) and returns four *signal categories*: `own_required`,
`own_excluded`, `opp_required`, `opp_excluded`. Each module then reduces that
SAME signal set with its OWN, deliberately different, precedence:
`biomarker_gating.py` has REQUIRED win on any conflict (a census feeds a count
a human will narrow by reading the sample — undercounting hides a trial from
that review entirely, overcounting costs one extra glance); `biomarker.py` has
a genuine conflict resolve to UNCLEAR, never a pick (asserting ELIGIBLE against
self-contradictory text is a false reassurance one patient could act on).
Neither policy is "more correct"; what would be a bug is the two modules
reaching OPPOSITE conclusions on ordinary, non-contradictory text —
`tests/test_markers.py::test_the_two_modules_never_reach_opposite_conclusions`
checks exactly that, on the same fixtures, comparing normalised directions
(ADMITS/EXCLUDES/SILENT) rather than literal strings, because the two
vocabularies use different literal strings by design (`biomarker.py`'s are
space-separated for a patient-facing table; `biomarker_gating.py`'s are
underscore-separated SQL LIKE tokens) even where they overlap.

**Negation is now recognised at a distance and as a suffix, not just
immediately before the marker.** The old check was anchored right before the
marker (`...not\s+$`), so STELLAR-303's real inclusion line — "Documented NOT
to have microsatellite instability-high (MSI-high)" — was never recognised as a
negation at all (two words, "to have", sit between "NOT" and the marker), and
`biomarker.py` had no negation handling whatsoever. `markers._negated` now
checks three positions: BEFORE the marker with up to three intervening words
(`_NEGATION_BEFORE`), AFTER the marker as a tight suffix — "RAS wild-type",
"RAS WT" (`_NEGATION_AFTER`) — and WITHIN a match whose own pattern already
requires a qualifier, like HER2_AMP's ("HER2-negative" is consumed as one match
by the base pattern rather than "HER2" + a separate suffix, so
`_NEGATION_WITHIN` checks the tail of the match itself). Suffix negation is at
least as common as prefix negation in real CRC eligibility text — MOUNTAINEER-03
requires "RAS WT" verbatim — and this generalises to every curated marker, not
only MSS: `tests/test_markers.py::test_wild_type_never_requires_the_marker_for_every_curated_marker`
checks all seven.

**A marker restated twice in one sentence is one statement, not two — negation
is decided once per (sentence, key), not once per regex match.** "documented
NOT to have microsatellite instability-high (MSI-high)" names the same marker
twice: spelled out, then abbreviated in parentheses. Classifying each
occurrence independently found the negation on the first (close enough to
"NOT") but not the second (the bounded window does not reach across the
parenthetical), producing a REQUIRED-and-EXCLUDED self-contradiction from a
single, unambiguous sentence — this was caught by testing the fix against real
registry text before shipping it, not by inspection. `collect_signals` now
finds all matches of a key within a sentence first, then classifies the whole
sentence once, negated if ANY occurrence shows a detected negation.

**A sentence that mandates a TEST is not a sentence that states a RESULT.**
"The tumor must have been assessed for microsatellite instability high (MSI-H)
or deficient mismatch repair (dMMR) status per a standard local testing
method" — C-800-25's actual inclusion line — names a marker without saying
which way it resolves. Reading it as REQUIRED overruled the trial's real
exclusion criterion two lines later ("Tumor is MSI-H/dMMR"). `_is_test_requirement`
recognises a determination verb (documented/assessed/tested/evaluated/determined)
near "status" with no direction-word between them and lets that sentence
contribute NO signal for any marker. The gap between verb and "status" is
unbounded-ish (150 chars) rather than tight, because the negative lookahead
already forbids any direction word appearing in it regardless of length — the
accepted tradeoff is a handful of rare sentences that DO carry a direction after
"status" (none observed in any fixture measured against real registry text)
neutralised in exchange for eliminating a demonstrated, silent inversion.

**A curated marker's verdict and an uncurated guess must never look equally
confident.** `config/markers.yaml` holds the reviewed marker table — same
principle as `config/trial_queries.yaml`: vocabulary is a clinical judgement, not
model-generated at query time, and a clinician can add FGFR2 fusions by editing
YAML. A biomarker string that resolves to no entry there falls back to
`biomarker._literal_match` — plain substring search, no negation handling, no
reviewed synonyms — and can only ever return UNCLEAR or NOT MENTIONED, never a
confident ELIGIBLE/ELIGIBLE BY EXCLUSION/EXCLUDED.
`BiomarkerMatch.curated`/`MarkerFlag` carry this so a reader is never shown an
unreviewed guess with the same weight as a reviewed marker's verdict; the
trial-landscape page surfaces a warning when the typed biomarker is uncurated.

**Two more registry fields were being fetched and silently dropped —
`detailed_description` and `keywords`.** ADG126-P001 states its MSS focus
("...with a focus on MSS CRC") only in `descriptionModule.detailedDescription`,
never in formal eligibility text; C-800-25 carries "MSS" and "Microsatellite
stable" verbatim in `conditionsModule.keywords`. Both modules were already
being requested (`DEFAULT_FIELDS` asks for the whole module), so this cost
nothing to fetch, only to parse — `TrialRecord` now carries both.
`markers.collect_signals` consults them, in order, ONLY when
`eligibility_criteria` (and each field before them) is completely silent for a
marker — never overriding a real eligibility-criteria statement with prose or
a keyword tag, which is why `record_texts` orders them
eligibility → detailed description → brief summary → keywords, least reliable
last. This bumped `STORE_VERSION` to 5 (`detailed_description`, `keywords`
columns). Checked and found clean: `identificationModule.officialTitle` and
`armsInterventionsModule.armGroups[].label/description` carry no marker text on
any of the six ground-truth trials — not fixed, since there is nothing to fix.

**A biomarker filter can now OR multiple statuses for one marker; every
question-set filter was audited for the old three-state assumption.**
`config/landscape.yaml`'s "MSS/pMMR trials specifically" question filtered
`biomarker: ["MSS:REQUIRED"]`, which — once ELIGIBLE_BY_EXCLUSION existed —
silently excluded STELLAR-303, C-800-25 and HARMONi-GI3: 3 of the 4 real MSS
mCRC trials this section exists to surface, all stating MSS only by excluding
MSI-H. `store.landscape()`'s `biomarker_filters` now accepts a list of statuses
per marker (`[("MSS", ["REQUIRED", "ELIGIBLE_BY_EXCLUSION"])]`), ORed together —
ANDing them, the naive fix, is unsatisfiable (a trial has exactly one status per
marker) and would have silently zeroed the section. Different markers still AND.
`diligence._biomarker_filters` groups same-marker YAML tokens into that OR list;
`config/landscape.yaml` now ships both tokens for MSS. The memo renders the two
states distinctly rather than merging them into one total — "gated to MSS
REQUIRED or ELIGIBLE BY EXCLUSION" plus an explicit "Of these: N REQUIRED, M
ELIGIBLE BY EXCLUSION" line — using `by_biomarker`, which already reflects the
filtered population (the per-status scalar query ANDs onto the same WHERE
clause), so no second query was needed, only saying out loud what was already
computed. Audited every filter in every question set for the same assumption:
`biomarker:` filters exist ONLY in this one question in `landscape.yaml`;
`diligence_questions.yaml` and `screening_devices.yaml` carry no `biomarker`,
`status`, or `phase` filters at all. Nothing else was under-counting.

**The fixed section's OWN denominator is not the same population as the
trial-landscape PAGE's, and that gap is a different, pre-existing, deliberate
filter — not a residual bug.** "MSS/pMMR trials specifically" also filters
`status: [RECRUITING, NOT_YET_RECRUITING]` (it is titled "which RECRUITING
trials require MSS"), while the page shows every status, open trials sorted
first. On the live store this means the SECTION's own scope holds 2 of the 6
ground-truth trials (STELLAR-303 and C-800-25 are ACTIVE_NOT_RECRUITING today),
while the underlying population — the same query-set-plus-biomarker-OR-filter
computation with no status narrowing, which is what the page runs — holds 4 of
6, matching the page exactly and confirming the fix is correct and consistent
across both surfaces. The section is also a k=30-capped SAMPLE of a 431-trial
population (the "showing N of M" pattern used everywhere else in this codebase
for a census), so even the 2 in scope are counted in its total without
necessarily appearing as a printed row. Widening the status filter, or listing
more of the sample, is a separate editorial call about what "MSS/pMMR trials
specifically" should mean — not bundled into this fix.

**Which trials print in an aggregate section's sample is a deterministic,
explainable relevance score, not "recruiting first, then completion date."**
Correct totals with an arbitrary-looking sample is its own failure mode: a
partner asking "why isn't STELLAR-303 in this table" is a worse question to
face than "is 431 right". `ranking.py` scores every trial in a section's
already-filtered population against `config/ranking.yaml` — phase, status,
enrolment, allocation (randomised/not), site count, start-date recency, and
whether a topic-specific `query.term` search (not just the broad condition
net) also found it — sums the points, and `Ranking.explain()` prints exactly
which of those fired and for how many points, one line per row. No model call
anywhere in the scoring path. `store.landscape()`'s sample query now does two
passes: a narrow SELECT (no eligibility text or other large TEXT columns) over
the WHOLE filtered population to compute every trial's score in Python, then a
second query for just the top `sample_limit` NCT IDs to build the full
records shown. This was a deliberate choice over building the score as a SQL
CASE expression: one Python implementation that both ranks the population and
explains the printed rows cannot drift out of sync with itself the way two
independently-maintained SQL and Python versions of the same scoring logic
could. Ties break on NCT ID, not a second unscored signal — anything that
decided a row's position has to appear in `explain()`, or the "every row
states why" rule is already broken for that row.

**The weights were written down, and justified on principle, BEFORE the six
ground-truth trials were checked against them — read `config/ranking.yaml`'s
header for the full reasoning.** Phase and status carry the highest weights
(the densest, most direct answers to "should this be read now"); enrolment and
randomisation are tied at a moderate weight (real, but a large single-arm
Phase 1 is still a Phase 1); site count is deliberately weighted below
enrolment because it is largely the same underlying fact — trial size —
counted twice; the query-provenance bonus is modest because in a
biomarker-filtered section the gating census, not this bonus, is the
authoritative relevance signal; recency is the weakest signal, a tiebreaker
rather than a driver. `sponsor_class` is excluded ENTIRELY, not weighted low —
it answers who is paying, not how urgently to read a row, and this codebase
already treats non-industry evidence as no less trustworthy elsewhere
(disclosures.py's independence axis); folding it into an urgency score would
bias toward industry sponsors for a reason that has nothing to do with
urgency. It stays visible on its own, in `by_sponsor_class`.
`tests/test_ranking.py::test_shipped_config_never_scores_sponsor_class` pins
the exclusion; `test_shipped_config_weights_phase_and_status_above_the_secondary_signals`
pins the stated priority order — both fail loudly if a future reweight drifts
away from the principle rather than deliberately overriding it.

Measured against the six-trial ground truth AFTER the scheme above was
written and shipped, not tuned toward it (2026-08-04, live store, 12,095
colorectal trials). CodeBreaK 301 and MOUNTAINEER-03 gate on KRAS G12C/HER2,
not MSS, and are correctly absent from either MSS population — ranking is not
a question that applies to them. Of the four that are:

```
mss-required SECTION population (its own RECRUITING/NOT_YET_RECRUITING filter): 431 trials
  HARMONi-GI3     rank  16 of 431   score 91 — Phase 3, open, 600 enrolled, randomised,
                                      140 sites, started <2y ago
  ADG126-P001     rank  74 of 431   score 77 — Phase 2, open, 186 enrolled, randomised,
                                      21 sites, term-query match, started <5y ago
  STELLAR-303, C-800-25: not in this population — excluded by the section's own
    status filter (both ACTIVE_NOT_RECRUITING), unrelated to ranking

UNFILTERED MSS population (no status filter, matches what the page counts): 826 trials
  HARMONi-GI3     rank  16 of 826   score 91
  STELLAR-303     rank  36 of 826   score 84 — Phase 3, active-not-recruiting, 901
                                      enrolled, randomised, 133 sites, started <5y ago
  ADG126-P001     rank  79 of 826   score 77
  C-800-25        rank 156 of 826   score 69 — Phase 2, active-not-recruiting, 234
                                      enrolled, randomised, 65 sites, started <5y ago
```

Only HARMONi-GI3 lands inside a k=30 sample under either population. This is
reported as a finding, not engineered around: the scheme, built on principle
before this measurement existed, systematically favours large, mature, open,
randomised Phase 3 trials — and three of the four ground-truth trials are
genuinely smaller or further from that shape (ADG126-P001 is Phase 2 with 186
patients; STELLAR-303 and C-800-25 are no longer recruiting and score lower on
status for exactly the reason status is weighted the way it is). The
uncomfortable implication worth naming plainly: a diligence tool whose ranking
rewards size and maturity will systematically bury the smaller, earlier,
more-differentiated competitive assets — arguably the ones a VC most needs to
see, not least because they are the easiest to miss by skimming a sample. This
is a property of which signals are available and how they were weighted on
principle, not a bug to patch by adding a rule that special-cases these six
trials; doing that would be the ranking equivalent of tuning the biomarker
matcher until recall hit six, and would not generalise to the next six trials
someone hand-checks.

**`allocation` (randomised vs not) was fetched and silently dropped, same gap
class as `detailed_description` and `keywords`.** `designModule.designInfo` is
already requested whole (`DEFAULT_FIELDS`) and carries `allocation` on every
live record checked, but `parse_study` only ever pulled `phases`,
`enrollmentInfo`, and `studyType` out of `designModule`. Now parsed and stored.
Bumped `STORE_VERSION` to 6. One thing worth naming precisely: `phase` is
stored POST-conversion (`parse_study` turns the API's `"PHASE3"` into
`"Phase 3"`, multi-phase into `"Phase 1/Phase 2"`, `"EARLY_PHASE1"` into the
inconsistent-looking `"EARLY_Phase 1"` — `.replace("PHASE", "Phase ")` only
touches the `PHASE` substring, leaving the `EARLY_` prefix alone). A ranking
config written against the raw API tokens silently never matches anything
and everything falls to the zero-point default — caught before shipping by
checking the SQL directly (`select distinct phase, count(*) from trials`
against the live store), not by reasoning about the conversion in the
abstract.

**The k=30 sample cap and the RECRUITING/NOT_YET_RECRUITING status filter on
the `mss-required` question are editorial decisions, left untouched.** Both
predate the ranking work and are not code decisions to make unilaterally. The
status filter excludes STELLAR-303 and C-800-25 outright (both
ACTIVE_NOT_RECRUITING today) — 2 of the 4 MSS ground-truth trials never enter
this section's population at all, regardless of ranking. The k=30 cap then
excludes 401 of the 431 that DO enter it, ranked below the top 30 — including
ADG126-P001 (rank 74), the smaller Phase 2 trial among them; only HARMONi-GI3
(rank 16, a large open Phase 3) survives both filters into the printed sample.
Both are named here, with the actual numbers, so whoever reviews
`config/landscape.yaml` can decide with the numbers in front of them, not
discover them by counting rows in a PDF.

**Biomarker state is a labelled column on the patient landscape, not a sort
key — the grouping was cancelling the ranking.** `build_landscape` sorted on
ELIGIBLE-before-ELIGIBLE_BY_EXCLUSION-before-UNCLEAR first and ranked not at
all, so on the live colorectal store the page returned 826 rows in which every
one of the 535 trials naming MSS directly outranked every one of the 188 that
state it by excluding MSI-H, regardless of phase, size or design: the
by-exclusion block began at row 536 and the unclear block at row 724. That put
STELLAR-303 at 693 and HARMONi-GI3 at 606 — two Phase 3 trials central to this
disease, below six hundred trials that matter less, for no reason other than
the grammar their sponsors used. All three admitting states now compete in one
list scored by `ranking.py`, ties broken on NCT ID, and every row prints its
own `explain()` line in a new "Why ranked here" column. The state stays on
every row with its criterion sentence, because an oncologist reads "excludes
MSI-H" and "requires MSS" differently and is entitled to — it is information
about the trial, not a priority over it.

`ranking.py` therefore now scores BOTH capped trial tables this tool prints,
where before it explicitly scoped itself to the diligence census and left the
patient page alone. One signal fires on only one surface: `proximity` (config
`signals.proximity`, tiers matching `landscape._proximity`'s 3 city / 2 state
/ 1 country), evaluated only when a caller passes `proximity_tier`, which no
diligence section does. It is deliberately weighted BELOW phase and status and
that is the arguable number in `config/ranking.yaml` — distance is the
patient's hardest practical constraint, but the match is an ungeocoded
substring test where "same state" can mean a six-hour drive.
`tests/test_ranking.py::test_shipped_config_keeps_proximity_below_phase_and_status`
pins it so a reweight has to argue in the YAML. With the location box empty the
signal is not evaluated at all rather than scored as zero — "not applicable
here" and "scored and found nothing" are different statements, the same rule as
`ValidationReport.assessed`.

**The patient landscape caps what it prints at 30, in `build_landscape`, and
says what the cap held back — including which states.** 800 rows is not an
answer. The cap lives in the data structure rather than in each renderer, so
the Streamlit page, the Markdown and the PDF show the same rows by
construction, the same reason `coverage.render_lines` is the only function that
turns a coverage statement into text. `TrialLandscape.sample_lines()` is that
one function here, called verbatim by all three surfaces. It breaks the
held-back rows down BY STATE, because "796 not listed" invites the assumption
that the excluded ones are the weaker by-exclusion and unclear rows, and on the
live store that is false — 11 of the printed top 30 are by-exclusion and 2 are
unclear, where the old ordering made the top 30 100% ELIGIBLE by construction.

30 is not derived from anything about patients: it is the number
`config/landscape.yaml`'s aggregate sections already use, adopted so the two
capped trial tables agree and "why does the page show more than the memo" is a
question nobody has to ask.
`tests/test_landscape.py::test_the_default_cap_matches_the_diligence_memos_sample_cap`
reads both and fails if they diverge. It remains an editorial call for whoever
owns that config — note that `limit` (what is SCREENED, a testing lever that
changes every count) and `show_limit` (what is PRINTED, with the whole
population still screened, counted and ranked) are different caps on purpose.

Measured on the live store after the scheme was in place, not tuned toward it
(2026-08-05, 12,095 colorectal trials screened, 826 admitting on MSS). The four
MSS ground-truth trials, old page ordering vs new:

```
                          state                    OLD   NEW  score
  HARMONi-GI3   ELIGIBLE BY EXCLUSION               606    16     91   (in the top 30)
  STELLAR-303   ELIGIBLE BY EXCLUSION               693    36     84
  ADG126-P001   ELIGIBLE                             10    79     77
  C-800-25      ELIGIBLE BY EXCLUSION               699   156     69

  first by-exclusion row: 536 -> 2      first unclear row: 724 -> 1
```

Three of the four rise sharply. The fourth, ADG126-P001, FALLS from 10 to 79,
and that is worth naming rather than burying: its old rank was an artefact, not
a judgement — inside the ELIGIBLE block the remaining tiebreakers were
enrolling-status, proximity and then NCT ID, so an alphabetically early NCT put
a 186-patient Phase 2 tenth on the page. It now sits at 79 with a printed score
of 77 that says exactly why. The count inside the 30-row cap is unchanged at
one of four, but the one that survives is different and is there for a stated
reason.

The ranks under the new ordering — 16, 36, 79, 156 of 826 — are identical to
the unfiltered-MSS-population ranks already recorded above for the diligence
census. That is the cross-check worth having: the page and the census now
select the same population AND order it the same way, so the same trial cannot
be 16th in one surface and 606th in the other.

And the uncomfortable implication recorded for the census applies here
unchanged, now to a patient-facing surface: a scheme that rewards size and
maturity puts three of four hand-checked MSS trials below a 30-row cap. Three
of them are genuinely smaller, earlier or closed to enrolment, which is what
the weights say they are. This is reported, not engineered around — special-
casing these four would be the ranking equivalent of tuning the biomarker
matcher until recall hit six.

**Every landscape output states its own coverage, on the page, not in a
footnote — `coverage.py`, one render function, three surfaces.** A curated
list of six trials says "here are six trials"; the coverage statement says
"here is the whole registry, here is what matched, here is what we cannot
see" — every number traced to a stored count or the registry's own
`countTotal`, never to what happened to land in a capped sample.
`render_lines()` is the ONLY function that turns a `CoverageStatement` into
text, and the Streamlit page (`theme.coverage_box`), the Markdown memo, and
the PDF all call it verbatim — "same numbers in all three" is true by
construction, not by three renderers remembering to stay in sync (which is
exactly how `biomarker.py`/`biomarker_gating.py` drifted apart before).

The first line is deliberately NOT a literal "X of Y" against a registry-wide
total, because there is no such number for a query SET: it is a union of
several independently-reported, heavily-overlapping queries (a MeSH-expanded
condition query already returns most of what its synonyms would), and summing
their reported totals inflates by roughly 4.5x on the real colorectal set —
putting that sum on the page as "the total" would be a materially misleading
number on a statement whose whole point is not to mislead. What the line
states instead is the property that is actually true and actually matters:
`IncompleteFetch` (trials/client.py) already guarantees every query in the set
was fetched to its own full reported total, or the ingest raised rather than
silently holding a subset — so "N of N, fetched {date}" asserts completeness,
not a fraction against an unverifiable denominator. When a query genuinely
failed (`query_coverage.errors`), the line names it and says the count is a
lower bound, rather than quietly presenting a partial number as whole.

"Not searched" is a static fact from `config/registries.yaml` (WHO ICTRP,
ChiCTR, EU CTIS, jRCT — none integrated), read differently from "searched,
found nothing": `CoverageStatement.ever_ingested=False` renders as "this tool
has not looked", never as "0 of 0", the same not-assessed-vs-nothing-found
rule as `ValidationReport.assessed`.

**The biomarker breakdown line ("N explicit, M by exclusion, K by synonym")
required a new axis the matchers didn't track: WHICH pattern fired.**
`markers.MarkerDef.canonical` — a subset of `positive`, the marker's own
literal name/abbreviation only ("MSS", "MSI-H") — lets `is_explicit_match`
tell a REQUIRED verdict decided by the literal name from one decided by a
synonym ("pMMR", "non-MSI-H"). Defaults to `positive` itself when a marker has
no real synonym variation (RAS/BRAF/HER2/KRAS_G12C/KRAS_G12D), so only
MSS/MSI-H — the only markers with genuine synonym variety — need the config
entry. `biomarker_gating.MarkerFlag.basis` (EXPLICIT/SYNONYM/NONE, the last
for every non-REQUIRED status) is computed in `_reduce` and stored as a new
`biomarker_basis` LIKE-token column (`gating_basis_tokens`), so "16 explicit,
23 by synonym" is a SQL COUNT, never a live re-scan — same principle as
`biomarker_gating`'s own tokens. `ELIGIBLE_BY_EXCLUSION` keeps its own
established name ("by exclusion") rather than being folded into this split;
it is a different axis (own-marker-named vs opposite-excluded), not a third
value of it. This bumped `STORE_VERSION` to 7.

**The breakdown line's population is the section's OWN population, not the
query set's — reusing `by_biomarker[marker]` here was a real bug caught before
shipping.** A diligence section can narrow beyond the query set (`mss-required`
also filters `RECRUITING`/`NOT_YET_RECRUITING`), and `store.landscape()`
already computes `total`/`by_biomarker` over the FULLY filtered population —
which, for the filtered marker itself, is scoped to
REQUIRED-or-ELIGIBLE_BY_EXCLUSION by construction. Reusing
`by_biomarker["MSS"]["NOT_MENTIONED"]` for the coverage line therefore always
reads zero: the WHERE clause it was computed under already excludes
NOT_MENTIONED trials by definition. `landscape()` now builds a SEPARATE
`base_clause`/`base_params` — condition/query_set/phase/status only, captured
BEFORE the biomarker filter is appended — and scores the breakdown against
that, so "explicit + by exclusion + by synonym + requires the opposite + not
mentioned" sums to the real population every time.
`tests/test_coverage.py::test_section_narrowed_by_status_gets_a_breakdown_scoped_to_its_own_population`
pins this, and was verified to fail (opposite/not-mentioned silently zeroed)
when reverted to reusing `by_biomarker`.

**The trial fetch runs to exhaustion and asserts against the registry's own
total.** `--max-records` is an explicit testing override with no default;
`iter_studies(max_records=None)` follows every `nextPageToken`. `run_query`
captures `countTotal` and raises `IncompleteFetch` — carrying both numbers —
when pagination yields fewer studies than the registry said it had. The old
default cap of 200-500 silently redefined the population as "whatever the API
returned first": a 500-record store of a 10,193-study colorectal query was
missing five of six known MSS mCRC trials, at ranks 1575-8594. Exhaustion is
affordable — a full 10,193-study fetch with every field is ~22s over 11 pages,
~21 MB of eligibility text — so there is no performance argument for the cap.
Studies the API counts but returns without an NCT ID are tracked separately
(`skipped_no_id`) so they cannot be mistaken for lost records.

**`IncompleteFetch` cannot fire when the PROCESS dies, so the ingest writes an
in-progress marker before the first network call.** Every completeness guard in
this codebase fires on a RESPONSE — `run_query` compares pagination against
`countTotal`, `CoverageReport.errors` records a query that threw. A killed
process raises nothing at all: the fetch stops, and a family holding 6,000 of
12,000 studies sits in the store looking exactly like a finished one. That is
the failure this whole tool exists to prevent, arriving through the one door
nothing was watching, and it was reachable because `query_coverage` was written
only AFTER a successful fetch — a row's existence meant success and its absence
meant nothing had happened, with no way to say "started".

`store.begin_ingest(qset)` now writes `status=IN_PROGRESS` before
`fetch_query_set` runs, and only `record_coverage` — which counts the store back
and checks every query against its own registry-reported total — can clear it.
Three states, not a boolean, for the same reason `ValidationReport.assessed`
exists: no row = never searched, IN_PROGRESS = started and nobody knows,
COMPLETE = verified. `verify_ingest` is the ONE implementation of "did this
finish", used by both the live ingest and the v8 backfill, so a rule added to
one cannot go missing from the other.

Two things worth stating precisely. First, `held` is COUNTED from the database
inside `record_coverage` rather than passed in: the claim is about the database,
so it has to be read from the database, and a caller handing in the number it
hoped for would verify nothing. Second, `begin_ingest` knocks a COMPLETE family
back to IN_PROGRESS when it is re-fetched — deliberately, because the moment new
records start landing the old recorded total no longer describes the store. The
previous numbers are kept in the row, so the coverage line can still say "N of
M" while saying the ingest did not finish.

**This also closed a hole that had nothing to do with crashing.**
`CoverageReport.complete` checked only for errors, never whether a query's
`fetched` reached its `reported_total` — and `--max-records` suppresses
`IncompleteFetch` by design, so a capped ingest recorded no error and graded as
a finished census. Same silent subset, reached through a documented flag instead
of a kill signal. `verify_ingest` checks both, so a capped run is now PARTIAL and
says so.

`coverage.render_lines` gained the branch, and the ORDER of its branches is
load-bearing: a named failing query prints before the generic partial line,
because "cond:bowel cancer did NOT complete" tells a reader which part of the set
is missing and "PARTIAL INGEST" does not. Both end at the same place — a stated
lower bound and the exact command that finishes it, from `_remedy`, written once
so no incomplete branch can report a shortfall without saying how to close it.

Schema v9 (`status`, `started_at`, `held` on `query_coverage`). The gap is
BACKFILLABLE and `migrate_derived_columns` grades existing rows from the numbers
they already hold — but only in the safe direction: a pre-v9 row may be called
COMPLETE only when its own recorded numbers prove it, and anything ambiguous
grades PARTIAL. A family wrongly told to re-run costs one fetch; a family wrongly
called complete is the thing being fixed. `python -m medrag trials --incomplete`
lists every set with its state and exits non-zero if any is unverified — that is
where a run interrupted by a crash resumes from. Families that were never
ingested at all are deliberately ABSENT from that list rather than reported as a
backlog: the store cannot tell "never searched" from "does not exist" without
the config, and the command says so instead of implying the list is complete.

Measured on the live store after the change (2026-08-08): all 9 ingested query
sets graded COMPLETE — stored count equal to `total_unique`, and every one of
their 40 queries recorded `fetched == reported_total`. The crash that prompted
this left no partial family, because the ingest happened to buffer the whole
fetch in memory and commit it in one transaction; a kill during the fetch
therefore lost the family entirely rather than half of it. That is luck of code
shape, not a guarantee — nothing stopped anyone from making `upsert` incremental
— and the window between `upsert` and `record_coverage` was real and unguarded
throughout.

**`resolve_marker` substring-matched a query onto the WRONG marker, and it was
live on a patient-facing page.** The old implementation fell back to
`any(a in norm or norm in a for a in mdef.aliases)` and returned the first
registry entry that matched. Two consequences, both measured on the live store:

  * `"KRAS G12C"` contains RAS's alias `"kras"`, so it resolved to RAS. The page
    answered **865** — every RAS statement in the colorectal set — where the
    correct answer is **70**. `KRAS_G12C` and `KRAS_G12D` were unreachable by
    any query at all.
  * `"MSI-H"` is a substring of MSS's alias `"non-msi-h"`, so it resolved to
    **MSS — the opposite marker**. Searching MSS and MSI-H returned
    byte-identical results (826 admitting, 261 excluded, same first row) and the
    page labelled both "MSS". An MSI-H patient was shown trials selected against
    them, with a criterion sentence printed as the evidence. One spelling away
    (`MSI-high`) it worked correctly, which is why nobody noticed.

The rule now: **the marker the page reports is the marker the user typed, or
nothing matched.** Exact equality against key or alias, after normalising both
sides identically (case, hyphen/underscore/slash → space); longest key first as
the tiebreak. Unmatched goes to the uncurated path, which can only return
UNCLEAR or NOT MENTIONED and says so. There is no third option and in
particular no silent substitution.
`test_no_query_resolves_to_a_marker_that_does_not_list_it_exactly` drives every
key, every alias and a set of adversarial near-misses and asserts the invariant
directly; verified to fail by reinstating the substring fallback.

Found by accident, which is the part worth remembering: it surfaced from a
PERFORMANCE gate — the census/live parity check written to prove a prefilter
safe — not from any test of the matcher.

**A sentence that ENUMERATES an assay panel is not a sentence stating a
result.** Second shape of the `_is_test_requirement` error. NCT05619172's
inclusion criterion reads "RAS wild type as confirmed by: locally performed
ctDNA assessment including at least mutations in exon 2 (G12D, G12V, G12C,
G12S, G12A, G12R, G13D)"; `\bG12C\b` matched inside that list, so the census
recorded KRAS_G12C REQUIRED for a trial whose actual requirement is the
opposite. `_ASSAY_PANEL` fires only on an assay noun followed by three or more
comma-separated variant-shaped tokens with no direction word between — narrow
enough that "Documented KRASG12D mutation in tissue" and "Subject has KRasG12C
mutation" stay genuine requirements.

**The biomarker census is a DERIVED column, so a matcher change forces a schema
bump.** v11 recomputes `biomarker_gating`/`biomarker_basis`/`biomarker_flags`
for all 241,298 records from stored text — no re-fetch. Leaving them would have
meant every landscape COUNT reflecting the old rules while the live screen
reflected the new ones, which is precisely the divergence the parity gate
exists to catch.

**Query-set membership is an indexed join table, and the token column was
DELETED rather than kept beside it.** `query_sets LIKE '% key %'` is a
leading-wildcard LIKE and cannot use an index, so every landscape search
full-scanned all 241,298 rows — six times, once per count/query/provenance/
census call — for a fixed **~3 seconds on every search regardless of family
size** (a 104-trial family took 3.1s). `trial_query_sets(set_key, nct_id)`
WITHOUT ROWID makes it an index range scan: 3.1s -> 0.12s on that family.
Schema v10. The migration builds the join table from the token column, ASSERTS
per family that both select exactly the same NCT IDs, and only then drops the
column — two sources of one truth is how `biomarker.py` and
`biomarker_gating.py` drifted apart in the first place.

**The census prefilter shipped only after being PROVEN equivalent, and the
proof is kept as a test.** `store.query(admits_marker=...)` narrows in SQL to
the trials the ingest-time census says could admit the marker, so the live
screen runs over 826 records instead of 12,095. That is exactly the
ingest-time-versus-query-time divergence this file warns about, arriving
disguised as an optimisation — so it was gated: all 74 families × 7 curated
markers, **2,150,918 record comparisons**, asserting the census admits exactly
what the live matcher admits.

The first run found **124 divergences in 62 families** and stopped the change.
All of them were `resolve_marker` and the assay panel, both fixed above; the
five unambiguously-resolved markers agreed everywhere from the start, which is
what said the prefilter itself was sound. `tests/test_census_live_parity.py`
keeps the equality enforced on fixtures in CI, and
`scripts/check_census_parity.py` is the full-store sweep to run before changing
either matcher.

Why the prefilter is safe in principle, not merely in measurement: the census
has no UNCLEAR — a conflict resolves to REQUIRED, which is admitting — so a
self-contradictory trial always survives the prefilter and reaches the live
screen that flags it. If the census ever resolved a conflict to EXCLUDED
instead, the prefilter would start dropping exactly the trials most worth a
human's attention, and
`test_a_self_contradicting_trial_is_admitted_by_both_paths` is what would fail.

Two counts must come from SQL under the prefilter rather than from the screened
subset, because the records they describe are deliberately never loaded:
`n_excluded`/`n_not_mentioned` from `biomarker_counts`, and
`n_no_eligibility_text` from `count_without_eligibility`. Inferring them would
report "0 excluded" for a population where hundreds are.

Measured, before and after: rett 3.1s -> 0.05s, colorectal 12.8s -> 1.8s, breast
~17s -> 1.9s. Throughput on two workers went from a flat 0.3 req/s that did not
improve with concurrency to 46 req/s on a cheap family and 1.3 req/s on
colorectal, now scaling with concurrency.

**The page and the health endpoint disagreed about the snapshot date.**
`public/data.snapshot_date()` returned the trials.db file mtime — when the
artifact was COPIED to the server — while `/healthz` read the embedded
`snapshot_meta`. On any real deployment the masthead every visitor reads would
have overstated freshness by however long the artifact sat before shipping.
Both now read the same embedded value. Found by the same audit; the class of
bug is two code paths answering one question, with the more visible one wrong.

**Every store WROTE to its own database just to be OPENED, and that was the
blocker for serving this publicly.** `TrialStore.__init__` ran
`executescript(SCHEMA)`, then `PRAGMA user_version = N`, then `commit()`, on
every open — including one that only ever runs SELECTs. `FDAStore` and
`DrugStore` were identical. Three consequences: a reader took a WRITE lock, so
opening the store during an ingest died with "database is locked"; on a
read-only filesystem the constructor did not start at all, so the pages failed
at import rather than degrading; and a public web app held write access to its
own data for no reason any read path needed.

`dbopen.py` is now the one place that opens a store for reading.
`TrialStore(path, read_only=True)` performs no mkdir, no schema, no version
write, no commit and no chmod, and `refuse_write` raises `ReadOnlyStoreError`
by name at the top of every writer. A missing file RAISES rather than being
created: an auto-created empty store answers every question "nothing found" for
a question nobody searched, which is the not-assessed-vs-nothing-found rule
arriving through the file layer.

**Two read modes, because the two requirements are not the same flag.**
`mode=ro` keeps normal locking, so the reader sees a consistent snapshot AND
picks up a concurrent writer's commits. `immutable=1` additionally promises the
file cannot change, which is what a genuinely read-only mount needs (nothing to
create — no lock file, no -wal, no -shm) and which SQLite documents as
undefined behaviour if the file does change. Measured on a connection held open
across a writer's commit: `mode=ro` 501 -> 2501 rows, `immutable=1` 501 -> 501.
Collapsing them into one flag would either break concurrent reads or risk
undefined ones, so `immutable` is an explicit assertion by the deployer that
this file is frozen.

**The concurrency test found a REAL defect, and it was the inverse of the
reported one.** With SQLite's default rollback journal a reader holds a SHARED
lock and a writer needs an EXCLUSIVE one, so they exclude each other: a page
polling the store starved an ingest until it HUNG INDEFINITELY (measured — the
test had to be killed at 5 minutes). The fix is `PRAGMA journal_mode = WAL` on
the writable path, where a writer appends while readers keep answering from the
last commit, plus a 10s `busy_timeout` on both so a momentary lock waits rather
than failing. After: 152 reads during an ingest, 0 failures, ingest 1.9s.
`test_the_writable_path_uses_wal_so_the_test_above_cannot_silently_regress`
pins the mechanism deterministically, because the concurrency test itself is
timing-sensitive.

Note the deployment consequence: a WAL database ships with `-wal`/`-shm`
sidecars, so a snapshot must be checkpointed (`PRAGMA wal_checkpoint(TRUNCATE)`)
before it is served, or the `.db` file is not self-contained. That is in
docs/RUNBOOK.md, not only here.

**`MEDRAG_READ_ONLY=1` is a separate flag, STRICTLY STRONGER than offline, and
it implies offline rather than the reverse.** Offline blocks outbound calls but
still lets the process write its own database, corpus, index and `.env`;
read-only forbids all of that. It is deliberately not inferred from whether the
mount happens to be writable — a deployment's guarantees must not depend on how
the volume was mounted that day.

**No fetch-on-miss anywhere reachable from a read path.** `ensure_data` (which
app.py and the claims page both call) used to fetch PubMed and the registry on
a miss, and `pages/3` fetched the whole query set for any uncached condition —
so a stranger typing a condition made this server pull tens of thousands of
studies. Both now check `cfg.read_only` FIRST, before any store is opened and
**before `force`**: a caller asking to re-download is asking for something this
deployment does not do, and the landscape page's "re-download" checkbox would
otherwise be a public button that triggers a full registry fetch.
`LoadReport.read_only` is kept distinct from `skipped` for the usual reason —
"the snapshot already covers this" and "nobody looked and nobody will" are
different answers, and the rendered wording says so ("this deployment does not
fetch … which is NOT a finding that it does not exist").

**Still unsafe on a public site, recorded rather than left to be discovered.**
Read-only mode does NOT fix these and they are named in docs/RUNBOOK.md: the
Settings "Change provider or key" button rewrites `.env` and mutates the server
process environment for every visitor; memo/PDF exports write to `out/` under a
filename derived from user input, so two visitors searching the same asset
collide (on the claims page the stem is the COMPANY NAME, so deck-derived
output lands at a guessable path); and app.py sends question text to the
configured LLM provider with no consent gate at all, while the claims page has
one.

**Transient registry failures are retried, and the retry is built so it can
never launder a failure into a success.** Measured, not anticipated: the
74-family ingest hit **41 HTTP 500s and 12 dropped connections** across three
passes, and every one of them downgraded an entire query set — a family is only
as complete as its least lucky query, so one blip anywhere in `breast`'s nine
queries cost a 14-minute re-fetch of thousands of studies that had already
arrived intact. Two families could not be verified at all across three attempts
for this reason alone.

The retry is per PAGE, inside `iter_studies`, which is the useful granularity: a
10,000-study query is eleven requests, and losing the eleventh to a 500 used to
discard the ten that had already succeeded.

**What is retried is deliberately narrow: 429, 500, 502, 503, 504, timeouts and
dropped connections. Never a 400 or a 404 — those are ANSWERS.** A 404 cannot
become a 200 by asking again, and a 400 is a bug in this tool that retrying
would hide behind a delay. `_RETRY_STATUSES` is pinned as a set by
`test_the_retryable_set_is_transient_failures_only`, so widening it to include a
4xx has to be a deliberate edit to a reviewed constant.

**The backoff floors are chosen against the Purple Book incident, not tuned for
speed.** 2s, 8s, 32s, with up to 50% jitter ADDED — never subtracted, because a
jitter that can shorten the floor undoes the politeness on exactly the retries
that matter most. `accessdata.fda.gov` answered three fast requests with HTTP
404 and a 420-byte Akamai bot-detection body, which this project nearly recorded
as "this source does not exist"; sub-second retry is what a scraper does, and
this is a source the tool depends on.
`test_the_first_backoff_is_seconds_not_milliseconds` pins the floor.
`Retry-After` outranks the local schedule when the server sends one, capped at
120s so a stray header cannot park an ingest for an hour — past the cap it gives
up and lets the family record PARTIAL, which is a state the tool can report
rather than one an operator has to sit and watch. Attempts are capped at 4 for
the same reason: unbounded retry converts "the registry is down" into "the
ingest never returns".

**An exhausted retry raises the ORIGINAL exception, and the family still records
PARTIAL.** This is the property everything else depends on. If an exhausted
retry could return a valid-looking empty response, `fetch_query_set` would
record a successful query with zero results and the status column phase 3 exists
to feed would be worthless.
`test_a_retry_that_exhausts_still_raises_rather_than_returning_empty` pins it,
and it was verified to fail by making the exhausted path return an empty
payload. The original exception propagates rather than a `RetryExhausted`
wrapper, because the coverage report, the CLI and the operator already read
those, and burying a 500 inside a new type would mean teaching every consumer a
new name for the same thing.

**Retrying is COUNTED and reported, on success as well as on failure — because
retry is precisely a mechanism for turning a loud failure into a quiet delay.**
`RetryBudget` counts attempts, retries, seconds slept and reasons (41 x HTTP 500
and 12 x ConnectionError are different diagnoses of the same registry, and
collapsing them loses that), rides on `QueryResult`, and is attached to the
EXCEPTION when a query dies — a query that failed after three retries and one
that failed immediately both record an error, and only the count distinguishes
"the registry is struggling" from "the registry said no". `CoverageReport.retry_line()`
prints on any ingest that retried at all, including one that then succeeded, so
"this took four minutes" and "this took four minutes because we retried forty
times" are not the same observation. Silence means the source was healthy; a
line printed every run would train a reader to skip it. The counts are also
stored per query in `query_coverage.yields`, so "was the registry healthy when
we fetched this?" is answerable from the database months later rather than only
from whatever scrolled past in a terminal.

`RetryBudget` deliberately exposes NO field or method meaning "complete" or
"succeeded", and there is a test inspecting `dir()` to keep it that way.
Completeness is `verify_ingest`'s call, in one place; a retry budget that also
graded outcomes would be a second opinion competing with it — the same drift
that put the marker vocabulary in `markers.py` after `biomarker.py` and
`biomarker_gating.py` disagreed about the same trial.

One thing worth stating precisely: `CoverageReport.complete` is the WEAK check
and now says so in its own docstring. It sees errors only. Whether a query
reached its `reported_total` is decided by `verify_ingest`, and that is what
grades a family.

**One condition string is a sample, not a population — ingest unions a reviewed
query set and records which query found each trial.** `config/trial_queries.yaml`
holds the synonym sets and is edited by whoever knows the field; it is never
model-generated at query time, because a synonym list that changes run to run
makes two ingests incomparable. `trials/queries.py` runs every query in the set,
unions by NCT ID, and reports **marginal yield** per query — when a synonym adds
nothing new, the set is measurably near-complete rather than hopefully so.
Provenance (`found_by`, a JSON array of `cond:…`/`term:…` labels) and set
membership (`query_sets`, space-padded tokens for LIKE) are stored per trial and
MERGED on re-ingest, never replaced: overwriting them destroys the answer to "did
we ever search for colon cancer?". `query_coverage` records what ran, mirroring
the FDA `catalog`. This bumped `STORE_VERSION` to 4.

Note the measured reality: `query.cond` is already MeSH-expanded server-side, so
`"colorectal cancer"` returns records registered as "Colorectal Neoplasms" and
"Colon Cancer", and the six colorectal phrasings agree on every non-basket trial
tested. The union is therefore cheap insurance rather than the fix — the fix was
the cap. Keep the union anyway: the cost is seconds and the failure it guards
against is silent.

**Basket trials are only partly reachable, and that gap is stated, never
inferred from absence.** A trial registered solely as "Advanced/Metastatic Solid
Tumors" (NCT05405595, which mentions "MSS CRC" only in its detailed description)
is absent from every colorectal condition query — this is structural, not a
missing synonym. `query.term` searches full text and does reach it (rank 166 of
391 for `"MSS colorectal"`), which is why sets carry a `terms` list; but a basket
trial that never names the indication anywhere is unreachable short of ingesting
`query.cond="solid tumor"` entire (9,699 studies to gain one). So
`queries.BASKET_CAVEAT` is stored in `query_coverage` and surfaced in the
landscape warnings and memo. An absent basket trial and a nonexistent one look
identical otherwise.

**The fetch defines the population; the local layer never re-narrows on the
condition string.** `build_landscape` selects by `query_set` — a structured fact
stamped on each record at fetch time — not by `LOWER(conditions) LIKE
'%colorectal cancer%'`. That substring match ran different logic from the fetch
and discarded trials the ingest had deliberately retrieved: MOUNTAINEER-03
registers "Colorectal Neoplasms", which does not contain "colorectal cancer".
Local filtering is on structured facts only — status, phase, sponsor class,
dates, biomarker. A screen that is capped says so (`population_total` vs
`n_condition`), because a silently truncated landscape is indistinguishable from
a complete one.

The **diligence census** (`_landscape_section` → `store.landscape`) follows the
same rule as of the same change: it passes `query_set=`, not
`condition=indication`. On the live colorectal store the substring version
counted 5,201 against a fetched population of 12,092 — it discarded 6,891 trials
(57%), and every headline denominator in a landscape memo was that number. The
memo now names the population it counted (`the "colorectal" query set`) rather
than echoing the reader's phrasing, because the two are no longer the same thing
and implying otherwise invites a substring reading nobody would endorse. A census
of zero because nothing was ingested is rendered differently from a census of
zero because nothing matched (`_empty_census_note`) — the same rule as
`ValidationReport.assessed`.

**All three consumers now select by query set.** `diligence._trials_for` and
`claims._retrieve` were the last two calling `store.query(intervention=asset,
condition=indication)`; both now pass `query_set=`. The indication a deck writes
("microsatellite stable metastatic colorectal cancer") is almost never a
substring of what a sponsor registered, so the AND collapsed to empty and every
claim fell through to the free-text fallback — the structured trial retrieval was
effectively dead for realistic indication strings. `store.query(condition=…)` no longer has a
production caller at all: the stopped-trial sweep was the last one, and it was
the fifth and worst instance of the defect rather than the sanctioned exception
this file claimed — see "The stopped sweep" below for the measurement that
overturned it.

**A drug name is a structured fact too — `agents.py` is the one matcher, and
`LIKE '%<asset>%'` over the interventions array was the same defect in a fourth
place.** `store.query(intervention=asset)` ran `LOWER(interventions) LIKE
'%<asset>%'` against a JSON array rendered as one string. The asset field takes
a human-typed phrase and most oncology assets are combinations, so the common
case was:

```
asset  = "botensilimab and balstilimab"
column = '["Botensilimab", "Balstilimab", "Oxaliplatin", ...]'
```

The phrase appears nowhere in that string — the array separator sits between
the two agents — so the query returned zero, and `_trials_for` and `_retrieve`
both fell through to free-text search without saying so. Measured on 200 real
two-drug trials drawn from the live colorectal set and phrased the way an
analyst types them, the structured path returned 74 trials in total and fell
back on 182 of 200 (91%). After: 6,699 trials, 0 fallbacks.

The array is now parsed, each element split into agent phrases, each phrase
normalised to a token, and matching is set membership — `intervention_tokens`,
a space-padded column written at ingest, the same scheme `query_sets` and
`biomarker_gating` already use. `STORE_VERSION` 8.

**Aliases expand at QUERY time, deliberately unlike the biomarker census.**
Stored tokens are the registry's own surface forms; `config/agents.yaml` maps
generic ↔ brand ↔ development code and is consulted when a query is built. So
adding "AGEN1181" takes effect against the database already on disk. Baking
canonical forms in at ingest — what `biomarker_gating` does — would mean a
12,095-record re-fetch for a vocabulary edit, and a vocabulary edit that
expensive is one nobody makes. The alias table is load-bearing, not decorative:
the registry carries `"Vectibix®"`, `"AGEN2034"` and `"XL092"` with the generic
name absent entirely, and no amount of string cleverness recovers
"balstilimab" from "AGEN2034". Curated pembrolizumab picked up 8 trials filed
only as `MK-3475` or `Keytruda`; leucovorin picked up 81 filed as folinic acid
or levoleucovorin.

**AND for selection, OR for the negative sweep — one matcher, two policies,
the same split as `biomarker.py` vs `biomarker_gating.py`.** A combination
ANDs its agents when selecting a population (the asset IS the doublet, and a
monotherapy trial of one half is a different asset). `stopped_trials` passes
`intervention_join="OR"`, because a terminated botensilimab-monotherapy trial
is exactly what a reader diligencing the doublet needs to see — the same
widen-rather-than-narrow rule `find_stopped_trials` already applies one level
up to intervention-vs-condition.

**ANDing introduces a new way to return zero, so the collapse names itself.**
One agent the registry never lists under any known name zeroes the whole query,
the fallback then fires and succeeds, and the collapse is invisible —
`store.query` returns rows with no denominator, the gap this file already
records for the fallback generally. `store.intervention_terms()` returns
per-agent counts and `agents.collapsed_combination_notes()` turns them into the
warning, shared by `_trials_for` and `_retrieve` rather than written twice.
"Every agent exists but the pair does not" produces NO note: that is a real
finding, not a matching failure.

**Hyphens are split BOTH ways, and that was a measured regression, not a
guess.** Auditing 25 agents against the old `LIKE` found 148 trials that
stopped matching. Most were hyphen-joined compounds — `Aflibercept-FOLFIRI`,
`FOLFIRI-cetuximab`, `Bevacizumab-IRDye800CW`, `l-Leucovorin` — where stripping
the hyphen produced `afliberceptfolfiri` and lost the agent. Splitting on
hyphens instead would have destroyed the codes where the hyphen IS the name
(`MK-3475`, `BAY 73-4506`), so a hyphenated word now contributes both readings.
That took the losses to 71. Every separator in `_SPLIT` is likewise attested in
live data, not anticipated: the ideographic comma is NCT06115733's
`"Fruquintinib、Capecitabine Tablets"`, `±` is NCT02948985's
`"FOLFIRI±cetuximab"`, the colon is NCT02271464's `"Maintenance:BEVACIZUMAB"`.

**68 of the 71 remaining "losses" are a precision GAIN: FOLFOXIRI is not
FOLFOX.** The old substring match had `'%folfox%'` matching FOLFOXIRI and
`'%folfiri%'` matching FOLFIRINOX — three distinct regimens conflated. Token
matching separates them, and `config/agents.yaml` lists FOLFOXIRI/FOLFIRINOX as
their own agent. Regimens are never expanded into their component drugs:
aliases are ORed, so listing oxaliplatin as a form of FOLFOX would return every
oxaliplatin trial — a silent widening, the same class of error as the substring
match this replaces.

The 3 genuine remaining misses across ~5,400 matches are all registry
malformations, and are recorded rather than papered over:
`"Bevacizumabl"` (trailing typo), `"Folinic Acid andIrinotecan"` (missing
space), `"21Capecitabine"` (digits glued to the name). The same class costs one
trial on the reported asset itself — NCT06751524 files `"Balstililmab"`, so it
is absent from a `botensilimab and balstilimab` search (it was absent before
too). **No fuzzy or edit-distance matching was added**, deliberately: drug names
differ by one or two characters routinely and inventing matches between them in
a diligence tool is a worse failure than missing a typo'd record.

**`agents.py` knows nothing about trials, because drugsFDA needs the same
matcher.** The record side takes an iterable of free-text names, the query side
takes a typed phrase, and `collapsed_combination_notes` takes the report shape,
not a store. Two independently-maintained ingredient matchers is exactly the
drift that produced the biomarker bug.

**A schema bump whose new column is DERIVABLE gets a backfill, not a
re-fetch.** `intervention_tokens` is a pure function of the `interventions`
array v7 already held, so `migrate_derived_columns` recomputes it in place —
12,095 records in ~1s, no network — and `python -m medrag trials --migrate` is
what the refusal message now points to for such a gap. `_BACKFILLABLE_FROM`
lists only the versions where this is honest; every other gap still gets the
fail-closed "delete and re-ingest", because v4's fetch provenance and v5's
detailed_description hold data only a re-fetch can supply and inventing them
would be worse than the refusal.

**Remaining `LIKE '%…%'` over free text, all of them, audited 2026-08-05.**
Recorded here so absence from this list means "does not exist", not "not
looked at":

| Where | Column | Kind | Status |
|---|---|---|---|
| `store.query` | `conditions` | free-text ARRAY | **FIXED** — the stopped sweep now selects by `query_set`; the parameter survives for a caller that genuinely wants a substring, and no production path is one |
| `store.landscape` | `conditions` | free-text ARRAY | not fixed; production callers pass `query_set=`, this is the legacy path |
| `store.query` | `lead_sponsor` | free-text scalar | not fixed; substring is defensible for a "contains" on a company name, but note the FDA applicant lesson — the same firm files under several names |
| `store.query` / `store.landscape` | `phase` | controlled-vocabulary scalar | correct by design: `phase="Phase 2"` is MEANT to match "Phase 1/Phase 2" and "Phase 2/Phase 3" (9 distinct values, all enumerated) |
| `fda/store.clearances`, `category_total` | `device_name` | free-text scalar | not fixed; the join key is `product_code` by deliberate design and `device_name` is a convenience lookup |
| `query_sets`, `biomarker_gating`, `biomarker_basis`, `intervention_tokens` | — | space-padded TOKEN columns | correct by design — this is the pattern, not the bug |

**The stopped sweep: the exemption was wrong, and the measurement replaced the
reasoning.** This file used to say the `conditions` substring was "kept only for
`stopped_trials`, where it is ORed with intervention to WIDEN a negative-evidence
sweep — that one is correct and must stay." The argument was that a substring is
loose, so the only risk is a few extra rows a reader can dismiss. That argument
was never measured, and it is false. On the live store
`LOWER(conditions) LIKE '%colorectal cancer%'` selects 5,201 of the 12,095-trial
colorectal set (43%), and for the negative sweep specifically it saw **557 of
1,336 stopped trials, missing 779 of them (58%)** — because "Colorectal
Neoplasms" does not contain "colorectal cancer". A substring over a free-text
array does not widen; it narrows, unpredictably, on exactly the records MeSH
expansion was fetched to reach. The one subsystem in this tool that is supposed
to be exhaustive by construction was its least exhaustive path. Fixed: the
indication arm selects by `query_set`, like every other consumer.

**Fixing it made the indication arm 2.4x larger, which is when a shared budget
starts hiding the arm that matters — so the arms were split, and the FIRST
SPLIT WAS WRONG.** Reserving 15 of the 25 rows for the intervention arm reads
as protective and is not: a reservation is a ceiling as much as a floor. For
"encorafenib and cetuximab" the old shared budget happened to yield 20
intervention rows, so a 15-row reservation dropped 5 of them, and across five
real assets **23 intervention-derived trials that had been shown stopped being
shown**. That is the measurement the split existed to prevent, produced by the
split itself.

The arms are now sized by their nature, not by dividing 25. The intervention arm
is bounded by the world — a compound has as many trials as it has, 93 at the top
of what was measured — and every one is a direct answer, so it keeps the whole
original budget of 25 and can never lose a row a shared budget would have shown.
The indication arm is effectively unbounded (1,336, and it grows with the fetch),
it is context rather than a finding about this asset, and no sample size makes a
1,336-row pool representative — so it gets a fixed 10 and states its denominator.
Worst case a section grows from 25 rows to 35. There is deliberately no spillover
between them: letting an empty intervention arm inflate the indication arm to 35
trades a longer memo for more rows of a sample that was already unrepresentative
at 10, and the coverage line carries that fact in one sentence instead.

**The within-arm tiebreak turned out to decide more than the budgets did.** With
the arms split and sized correctly, 20 intervention trials STILL fell out of
view for one asset — not from starvation (it showed 25, up from 20) but because
"which 25 of 81" was decided by NCT ID, and 89% of these trials carry a stated
reason, so the reason-first sort decayed to alphabetical: oldest-registered
first, the opposite of what a diligence reader wants, and unstable under any
change to how many candidates were fetched. Ordering by start date descending
(most recent failure first, a trial with no date on file sorting LAST rather
than leading as if it were newest) fixed it and made the candidate window
invisible — fetching 70 and showing 25 now yields the same 25 as fetching 25.
`test_which_rows_are_shown_does_not_depend_on_the_candidate_window` pins that.

Measured on the live store, five real assets, before vs after
(2026-08-05, 12,095 colorectal trials, 1,336 of them stopped):

```
                              total found      shown     from compound   from indication   dropped
  botensilimab+balstilimab    559 -> 1,336    25 -> 12      2 ->  2          23 -> 10          0
  encorafenib+cetuximab       638 -> 1,336    25 -> 35     20 -> 25           5 -> 10          0
  regorafenib                 583 -> 1,336    25 -> 35     16 -> 25           9 -> 10          0
  fruquintinib                560 -> 1,336    25 -> 13      3 ->  3          22 -> 10          0
  sotorasib+panitumumab       574 -> 1,336    25 -> 27     17 -> 17           8 -> 10          0
```

`dropped` is the number that decided the design: intervention-derived trials
that were shown before and are no longer shown. It is 0 for every asset, and the
intervention arm shows strictly more than before on every asset where it was
being truncated. The two assets whose totals fell (25 -> 12, 25 -> 13) lost only
indication-context rows, replacing 22 arbitrary rows of 557 with 10 arbitrary
rows of 1,336 plus a stated denominator — the previous 22 were never a sample of
the real pool, because the real pool had not been found.

`StoppedTrialSweep.coverage_line()` is the one function that renders the split,
called by the Markdown memo and the PDF, and each printed row is labelled "this
compound" or "this indication" — a compound that failed elsewhere and a disease
that is hard to treat are different findings and a reader should not have to
infer which one a row is. `searched_intervention`/`searched_indication` keep an
arm nobody ran distinct from an arm that found nothing, per arm, the same rule
as `ValidationReport.assessed`.

One thing deliberately left: the indication arm's 10 rows are a sample of 1,336
chosen by "stated reason, then most recent". That is defensible and stated, but
it is not the deterministic relevance score `ranking.py` already applies to the
landscape's capped sample. Wiring `ranking.py` into this arm is the obvious next
improvement and was not bundled here.

**The free-text fallback fires only on an empty result, so a partial drop is
undetectable.** `if not records: records = store.search(...)` in both
`_trials_for` and `_retrieve`. Structured returning 0 of 214 falls back and is
visible; returning 6 of 214 does not fall back and is indistinguishable from
"6 is all there was", because `store.query` returns rows with no denominator.
This bit a test during the fix: a regression fixture holding only the unmatched
trial passed with the bug still in, because FTS rescued it. The discriminating
fixture has to hold one trial the substring DOES match, so the result is
non-empty and the fallback cannot fire. Not yet fixed; the shape of the fix is a
matched-total return (`store.landscape` and `run_query`/`IncompleteFetch` already
do exactly this at their layers).

**The relevance floor only ever gated LITERATURE, and half the evidence path
walked past it.** Asked to diligence PBX-7749 in hidradenitis suppurativa — an
asset with no trial and no publication anywhere — the memo reported "sections
answered with evidence: 11/11" and cited transcutaneous-bilirubinometry
meta-analyses as efficacy evidence and eight colorectal trials, each with a real
NCT ID. Raising `score_floor` 0.05 → 0.35 took it to 9/11, not 0/11, because
trials reach the memo through SQL and FTS, which carry no similarity score and
so no threshold can touch them. At 9/11 the thin-evidence banner never fired and
the memo read as evidenced.

The registry half is fixed in `trials/anchors.py`, which is the SQL equivalent of
the floor: a query must be ABOUT something. Three separate paths produced those
eight trials, all closed there — a `store.query` with nothing to filter on
degrades to `SELECT * FROM trials LIMIT k` (reachable two ways: an indication
resolving to a set the store never ingested, and an asset `agents.parse_asset`
cannot parse, where `_intervention_clause` returns `1=1` by design); an
un-ingested query set yields zero rows that read identically to a family searched
and found empty; and the free-text fallback was handed
`f"{asset} {indication} {question}"`, so `store.search`'s OR matched on `trials`,
`other` and `run` and the asset name contributed nothing to a single returned
row. The fallback now searches the ANCHORS only and re-checks every row against
`agents.py` — a loose retrieval is allowed only with a strict check behind it,
the same shape as the census/live-screen parity gate. `TrialAnchor.notes()`
keeps never-searched distinct from nothing-found, per
`ValidationReport.assessed`. `claims._retrieve` had the identical code and
therefore the identical defect, and uses the same module rather than a second
copy.

**The floor was recalibrated against a real sample, and the two distributions
OVERLAP — the previous comment claiming they "separate cleanly" was written from
a single query pair and was wrong.** Measured on the real index (11 rendered
questions x 33 asset/indication pairs, 363 retrievals, three corpus families):
on-topic top-1 min 0.334 / p25 0.530 / median 0.643; off-topic min 0.177 /
median 0.361 / p95 0.482 / max 0.555. 45 of 143 on-topic scores fall below the
highest off-topic one, so no threshold admits all real evidence and no false
evidence — the number chooses which error to make. 0.50: on-topic sections 79%,
off-topic 2%, 17 of 20 absent assets fully silent. Deliberately NOT 0.45, which
would have zeroed PBX-7749 while leaving 7 of the other 20 absent assets
evidenced — tuning to the one asset that exposed the bug, the retrieval
equivalent of tuning the biomarker matcher until recall hit six. Where the
on-topic loss falls is what decided it: the eight questions whose answers live in
a published abstract keep 12-13 of 13 assets each, and the whole loss sits in
`mechanism`, `development-stage` and `competitive-trials`. Full table in
`config.py`.

**`competitive-trials` interpolates neither `{asset}` nor `{indication}`, so it
embeds to ONE fixed vector and scores an identical 0.436 for every asset in the
sample** — on-topic and off-topic alike. No floor can distinguish anything there
because the query contains nothing to distinguish; it is a question-set defect,
not a retrieval one, and fixing it belongs in
`config/diligence_questions.yaml`.

**`memo.warnings` was snapshotted BEFORE the sections ran**, so every warning a
section raised — `_warn_collapsed_combination`, `_flag_approval_overreach`, the
anchor notes — was appended to a list the memo had already copied, and reached no
reader at all. The convention this file states for guards applies to notices:
one production cannot surface is decoration. `run()` now re-reads the list after
the loop.

**`tests/test_retrieval_relevance.py` is the pin, and it exists because nothing
tested retrieval QUALITY.** The full suite passed unchanged when the floor moved
0.05 → 0.35 and would have passed at 0.0. The property is a pair — an asset with
no data returns no evidence, AND an on-topic asset still returns its evidence —
because either half alone is satisfiable by returning nothing or everything.
Every half carries a negative control asserting the fixture can still reproduce
the defect. Verified to fail both ways before shipping: at floor 0.05 five
assertions fail, and with the anchor check removed from `_trials_for` two do. The
stub embedder's cosines are the measured on-topic and off-topic medians, not
invented — the real weights are a 90 MB download CI does not have and the network
guard would refuse.

**THE FLOOR IS A CHOSEN TRADEOFF, NOT A SEPARATOR — read the distributions
before moving it.** Measured on the real index (11 rendered questions x 33
asset/indication pairs, 363 retrievals, three corpus families on-topic and 20
absent assets off), top-1 cosine per question:

```
                min    p05    p25    med    p75    p95    p99    max
  on-topic     0.334  0.433  0.551  0.644  0.742  0.829  0.897  0.897
  off-topic    0.177  0.234  0.311  0.361  0.401  0.482  0.521  0.555
```

**The two distributions OVERLAP.** 38 of 143 on-topic scores fall below the
highest off-topic one (0.555); 144 of 220 off-topic scores sit above the lowest
on-topic one (0.334). No threshold admits all real evidence and no false
evidence. So `score_floor` does not separate two populations — it decides which
of the two errors to make, and 0.50 is the point chosen for THIS corpus and THIS
embedder, favouring an empty section over a false one because a memo citing a
bilirubinometry meta-analysis as a hidradenitis drug's efficacy evidence is worse
than a memo with a gap, the same way an invented contradiction is worse than
silence.

Anyone moving this number should re-measure and look at the distributions rather
than reasoning about the number. `tests/test_retrieval_relevance.py` asserts the
overlap itself (`on_topic_below_highest_off_topic > 0`), so a re-measurement that
ever separates them cleanly fails the suite and forces this reasoning to be
rewritten rather than silently inherited.

**A question that interpolates neither `{asset}` nor `{indication}` is a
constant string, and `competitive-trials` was one.** "Which other sponsors have
run clinical trials on this mechanism or target" is an anaphor with no referent,
so it embedded to ONE fixed vector and returned an identical retrieval — an
identical 0.436 across all 33 assets measured — whatever was being diligenced.
Nothing detects that from outside: the section looks answered. Only the referent
was bound (`the mechanism or target of {asset}`); what the question ASKS is
unchanged, and it is still deliberately not scoped to `{indication}`, because
"who else has worked on this target" is a broader question than "in this
disease" — changing that is an editorial call for whoever owns the file.

Worth recording: the constant question was COSTING recall, not adding it.
Binding the referent raised on-topic retention at the shipped floor from 79% to
87% and on-topic p25 from 0.530 to 0.551, with off-topic unchanged at 2%.
`test_no_shipped_question_is_a_constant_string` sweeps every `questions:` YAML
under `config/` — discovered, not listed — and it is the only one of the 23
shipped questions across three sets that had the defect.

**A CONFIGURED provider that refuses is not the same as no provider, and only
the second was handled.** `make_client` returning `None` is the "no model" path
every caller degrades on. A revoked key builds a client fine and then raises
`openai.PermissionDeniedError` out of `client.chat.completions.create` — which
`Router.route` and `ContradictionHunter.hunt` already caught, and
`diligence._answer` and `Generator.generate` did not. So the documented
behaviour was two-thirds true and false in the third that mattered: the memo
died with a traceback on question 1 of 11.

`providers.call_chat` returns `(response, failure)` instead of raising, and
`describe_failure` BUILDS the message from the status code and provider name
rather than rendering the SDK exception, which prints the response body — the
same rule as `public/reqlog.RequestLogLine`, and for the same reason: this
string goes into a memo a human reads and may circulate.

400/401/403/404 latch the model off for the rest of the run; 429, 5xx, timeouts
and connection failures do not. That is `trials/client._RETRY_STATUSES` stated
from the other side — a 403 is an ANSWER, and asking eleven times produces
eleven identical refusals, eleven round trips, and eleven copies of one warning
that then bury everything else in the warnings block. Anything unrecognised is
treated as non-fatal, deliberately: failing open is safe here precisely because
the fallback is extractive evidence rather than silence. Tested by running a
real memo against a dead Groq key, not only against a mock.

**Staleness was computed at import, so the refusal only ever fired on a
restart.** `verify()` freezes `age_days` when the process boots. A service that
started one day inside the threshold and ran for a month kept serving and kept
answering `snapshot_stale: false` — the tool misdescribing its own state, which
is what `public/artifact.py` exists to prevent, arriving through the passage of
time rather than through a bad artifact. `ArtifactStatus.age_days_now()` /
`is_stale_now()` recompute from `snapshot_date`; `/healthz` calls them per
request and answers **503** when currently stale, because `verify()` refuses to
START on that data and a health check that keeps saying "ok" is what lets it go
on serving. `age_days` stays frozen beside it as
`snapshot_age_days_at_startup` — it records what the refusal acted on, and the
gap between the two numbers is how long the process has been up.

**A 500 left one access-log line and nothing to debug from, and the obvious fix
would have leaked.** `traceback.format_exc()` ends with the exception's
`str()`, and on the public service that string routinely quotes the visitor's
input — a `ValueError` echoes the value, `sqlite3` can carry a bound parameter,
`KeyError` names the key. Filtering the rendered traceback would be a blocklist,
which is how this leak happens in the first place. `reqlog.log_exception` BUILDS
the line from `traceback.extract_tb`'s structured frames: exception type plus
`file:line in function` per frame, never `str(exc)` and never a local. There is
no field that can hold a search term. The cost, stated: a `KeyError` no longer
says which key; the file and line do. Registered as an
`@app.exception_handler` rather than handled in the middleware, because the
middleware sits outside the exception — `call_next` raises, so its `log_request`
never runs for the failing request.

**The trials.db schema is versioned and a stale database is refused.** The
patient-landscape columns (eligibility, contacts, locations) bumped
`store.STORE_VERSION`; `TrialStore` writes it to `PRAGMA user_version` and refuses
to open an older file with a `TrialStoreSchemaError` carrying a rebuild step,
rather than reading a table with columns silently missing — the same fail-closed
choice `vectorstore.INDEX_SCHEMA` makes.

**The claims table and the trial-landscape table share one renderer.**
`table_render.py` owns the Markdown pipe-table and the reportlab table; both
`claims_memo.py` and `landscape_memo.py` build rows and hand them over, so the two
outputs cannot drift.

**A reportlab table overflows the page two ways, only one of which raises —
both are handled in `table_render.py`, once, for all three renderers.** The
landscape PDF crashed on the first real run (`LayoutError` out of
`landscape_memo.render_pdf`) because a Table splits only BETWEEN rows by
default, so a row taller than the frame has nowhere to go and reportlab aborts
the whole document rather than that row. This is not an edge case for this
tool: the landscape prints each trial's verbatim eligibility criterion, and the
longest on the live colorectal store is 2,627 characters — a 772-point row in a
513-point frame. `splitInRow=1` lets such a row break across a page boundary.
Truncating the cell was rejected: the criterion sentence is the evidence the
row exists to show, and clipping it silently is the same failure class as
silently dropping trials.

The second way is the dangerous one because it does NOT raise. Reportlab draws
an over-wide table straight off the right edge of the paper, so a column budget
that overruns the frame produces a PDF whose last column is simply missing,
with no error anywhere. `pdf_table` now takes `available_width` and scales the
budget to fit — but scaling is the backstop, not the plan:
`tests/test_pdf_render.py::test_no_renderer_builds_a_table_wider_than_its_own_page`
sweeps all three renderers and asserts none of them needs it, so an added
eleventh column is caught in CI rather than discovered as a cropped page. Each
renderer now states its page geometry once (`PAGE_SIZE_IN` / `SIDE_MARGIN_IN` /
`AVAILABLE_WIDTH_IN`) and both the doc template and the column budget read it,
so a margin change cannot silently invalidate the widths.

Worth stating precisely, because the reported symptom implied otherwise: the
suite DID build PDFs before this — `test_landscape`, `test_claims` and
`test_diligence` each render one and check the `%PDF` magic. All three passed
throughout, because all three render short fixture rows and this failure needs
a row taller than a page. `tests/test_pdf_render.py` exists to feed every
renderer the shapes that actually break one (a page-busting cell, a 500-char
unbreakable token, XML-hostile characters, empty cells), and its width sweep
carries a companion test asserting the sweep actually reached all three
renderers — which immediately earned its keep: the diligence PDF builds no
table at all on an ordinary memo (both its tables live in `_aggregate_pdf`, for
census sections only), so the sweep was covering it vacuously until it was
driven with a real aggregate section.

**openFDA is a third structured store, matched on product code — never company.**
`fda/` mirrors `trials/`: clearance status, product code and device class are
filters, so they live in indexed SQLite, not the vector index. The join key is
`product_code`, because attribution by manufacturer is unreliable on live data —
the same firm files under "Baxter Healthcare Corp" and "Baxter Healthcare
Corporation", and acquisitions scatter a product line across subsidiary names, so
matching on `applicant`/`recalling_firm` would silently miss clearances. A device
name resolves to its product code(s) via the clearances table, and recalls/events
are looked up from there. The three endpoints do NOT share a schema: `device/510k`
and `device/recall` carry `product_code` at the top level, but `device/event`
nests it at `device[].device_report_product_code` (a top-level `product_code:`
search 404s), and `event.date_received` is `YYYYMMDD` while the others are ISO.
Multi-term searches join with `" AND "`, never `"+AND+"` — a literal `+` is
URL-encoded to `%2B` and breaks the query. `FDAStore` is versioned via
`PRAGMA user_version` and refuses a stale DB, from the start.

Two things the memo must always say when it shows clearances, both because the
raw numbers mislead. First, the **sample against the total**: a category holds
hundreds of clearances, the store holds a capped sample
(`cfg.fda_max_clearances`), and the local store is itself a capped ingest — so
the memo says "showing N of M held locally … openFDA reports T cleared in this
category", where T is the API-reported total captured at ingest (`count_510k`,
stored in the `catalog` table). Second, the **applicant over-count caveat**: the
`applicant` string is stable but the *company* is not — Imed/Alaris/CareFusion/BD
are one product line under four names — so a count of distinct applicant names
over-counts distinct companies, and the memo says so wherever it shows a
clearance count. Without it a consolidated market reads as a fragmented one.

**openFDA DRUG coverage is a fourth structured store, matched on active
ingredient — and the obvious matching field would have silently dropped 57% of
the database.** `fda/drugs.py` + `fda/drug_store.py` sit BESIDE the device store,
not inside it: a 510(k) clearance and an NDA approval are different regulatory
objects with different identifiers, and merging them would put a nullable half
of each schema in every row. Reconnaissance was done against the live API before
any code was written; every number below is measured (2026-08-05), not assumed.

**The matching field is `products[].active_ingredients[].name`, NOT
`openfda.generic_name`.** The `openfda` block is a convenience join derived from
SPL linkage and it is simply absent from most of drugsFDA:

```
products.active_ingredients.name   28,904 of 29,252 applications  (99%)
openfda.generic_name               12,488 of 29,252 applications  (43%)
```

NDA017488 (Modicon) carries no `openfda` block at all yet states ETHINYL
ESTRADIOL and NORETHINDRONE in its products. Measured across this tool's own
42-agent asset list the two fields find the same 31 assets, but the ingredient
field reaches **261 applications against openfda's 138 (+89%)** — leucovorin
59 vs 25, fluorouracil 52 vs 23, oxaliplatin 33 vs 14. The difference is
generics and biosimilars, which is exactly the competitive picture a diligence
memo asks about. This is the same class of error as the condition-substring
bug: reaching for the field that looks canonical instead of the one that is
populated.

**Approval is a SUBMISSION fact, and there are three states, not two.**
`submissions[]` carries `submission_type` (ORIG | SUPPL) and `submission_status`
(AP 25,490 | TA 1,140). The original approval is the ORIG row. **TA is Tentative
Approval and is NOT approval** — the FDA found the application met requirements
but could not approve it, usually for patent or exclusivity reasons. A SUPPL row
is an efficacy supplement and can never make an unapproved application approved.
`products[].marketing_status` is a fourth, orthogonal axis (Discontinued 14,762,
Prescription 13,382, None (Tentative Approval) 716, OTC 610): an
approved-then-withdrawn drug is not an unapproved drug, and `_drug_block` says
so on its own line rather than letting prose blur it.

**"Not found in drugsFDA" never renders as "not approved", and that is enforced
at the type level.** `DrugStore.approval_answer()` is the only way to ask, and
it returns an `ApprovalAnswer` with no boolean called `approved`: `is_approved`
requires positive evidence (a matched application whose ORIG submission was AP)
and is False both when nothing matched and when the store was never searched —
which `statement()` renders as three visibly different sentences. Absence
carries `ABSENCE_MEANINGS`, the four things it can mean: never submitted;
submitted and not approved; approved under a name we did not match; approved
outside the US. One wording detail is load-bearing: an earlier draft said "this
is NOT a finding that the asset is unapproved", which *contains* the phrase "is
unapproved" — a downstream tool matching on text, or a reader skimming, sees the
claim rather than its negation. It now reads "absence from this database says
nothing either way about approval status".
`tests/test_fda_drugs.py::test_no_renderer_turns_an_empty_result_into_an_approval_claim`
drives the store, the assembled context and the provenance summary with an empty
result and asserts no surface produces an approval or a disapproval; three tests
fail immediately if absence is made to render as non-approval.

**Ingredient matching is `agents.py`, extended not forked.** `ingredient_tokens`
is `agents.token_blob()` over every name an application can be found under,
filtered with the same space-padded LIKE scheme as `query_sets` and
`intervention_tokens`, ANDing the agents of a combination exactly as the trial
store does. The one extension needed was salts: drugsFDA writes the counterion
into the name, and measured over the 1,000 most-used ingredient names, 568 are
multi-word with HYDROCHLORIDE alone accounting for 181 trailing words. One
direction already worked (word tokens meant "irinotecan" found "IRINOTECAN
HYDROCHLORIDE"); the reverse did not. `_salt_base` closes it on both sides,
against a `salt_forms` vocabulary in `config/agents.yaml` derived from that
count. Nothing is stripped — the base is ADDED as an alternative reading —
because the trailing word is not always a counterion: "POTASSIUM CHLORIDE" and
"SODIUM CHLORIDE" ARE the drug, and stripping would turn one into a different
substance.

**Matching locally beats matching through the API's query syntax, and the
combination case proves it.** drugsFDA files each ingredient of a combination
product as a separate `active_ingredients` entry, so a live
`products.active_ingredients.name:"trifluridine tipiracil"` returns 404 while
"trifluridine" returns 7 and "tipiracil" returns 4. Against the local token
blob, all seven phrasings — the pair, the brand (Lonsurf), the code (TAS-102),
either ingredient alone, and the salt form "tipiracil hydrochloride" — resolve
to the one application.

Measured against this tool's 42-agent asset list: **32 of 42 (76%) are findable**.
Every one of the 10 misses is a correct absence, not a matcher failure: four are
REGIMENS (FOLFOX, FOLFIRI, FOLFOXIRI, CAPEOX) for which no application can
exist, and six (botensilimab, balstilimab, divarasib, ivonescimab, muzastotug,
zanzalintinib) are investigational agents with no US application — which is
precisely the population for which "not found" must never read as "not
approved".

**What is NOT integrated, stated rather than left to be inferred.** The bulk
exports agree with the API totals exactly on all four endpoints, and drugsFDA is
a single 8.9 MB partition — exhaustively ingestible, though the current CLI
fetches per asset. The label corpus is 1.76 GB across 14 partitions and is
fetched per asset with every section capped at `MAX_SECTION_CHARS` and the
truncation recorded on the record. **FAERS (`drug/event`, 20,692,690 reports,
113 GB) is not integrated at all** — a declared gap, not an oversight. Two
linkage realities limit what can be joined: only 74,827 of 261,379 labels (29%)
carry `openfda.application_number` (`DrugLabel.linked` says which case a label
is), and only 3,169 of 17,860 drug recalls (18%) do, so recalls are matched on
product-description text. openFDA returns **no rate-limit headers** on any
response, so remaining quota cannot be read back; both drug and device clients
share `client._throttle` deliberately, because they draw on the same 240/min
per-IP bucket and two independent throttles would each think they had all of it.

**The approval statement is RENDERED, not written — the model may write around
it and may not write it.** `ApprovalAnswer.render_lines()` is the only thing
that turns a regulatory answer into prose, and `memo.py` inserts those lines as
a FIXED string in both Markdown and PDF, before the model's paragraph. The
reason is the whole point of the feature: every guard around that object —
`is_approved` requiring positive evidence, the four meanings of absence,
tentative-approval-is-not-approval — is a guard in CODE, and a model handed the
object as context and asked to summarise it converts "no application matched"
into "not approved in the US" in one paraphrase, walking past all of them. The
applications themselves still become numbered evidence the model can cite; the
STATUS is not its to write.

Three layers, because a prompt instruction is a request rather than a guarantee:
`APPROVAL_PROMPT_GUARD` tells the model the status is already stated and to
leave it alone; `_flag_approval_overreach` checks the generated prose against
`_NON_APPROVAL_PHRASES` whenever the deterministic answer does not support such
a claim, and raises a loud memo warning rather than letting two paragraphs of
one section contradict each other; and
`tests/test_memo_approval.py::test_a_memo_for_an_investigational_asset_never_implies_non_approval`
runs a real memo end to end for botensilimab and asserts the RENDERED output
contains no such phrasing. That test was verified to fail by writing
"Botensilimab is not approved in the United States." into the section.

The forbidden-phrase list is deliberately duplicated in the test rather than
imported from `diligence.py`: it is the specification the memo must satisfy, and
a test importing the implementation's own list would keep passing if someone
quietly shortened it.

**Wording that shares a substring with the claim it denies is a bug, twice
now.** `ABSENCE_MEANINGS[1]` originally read "submitted but not approved (or
still under review)" — a true statement of one possibility, containing the
literal string "not approved", inside a block whose entire job is to avoid
asserting that. It now reads "submitted and still under review, or refused".
This is the same lesson as the earlier "this is NOT a finding that the asset is
unapproved" draft: a reader skimming, or a downstream text match, sees the claim
and not its negation, so the denial and the claim must not share a substring.
Both were caught by the tests rather than by inspection.

**Approval is four axes, not a boolean, and each is rendered separately.**
`axis_submission` (AP vs TA vs not stated, with the earliest approval date),
`axis_marketing` (Prescription / Discontinued / OTC counts — and an explicit
"approved and then withdrawn from marketing, which is a different fact from
never approved" when every product under an application is discontinued),
`axis_application_mix` (NDA/BLA originator vs ANDA generic filings, which is a
read on how contested the molecule already is), and `axis_label_history`
(approved supplements since original approval — a drug with 135 supplements
reads very differently from one with none). Collapsing these into "approved:
yes" destroys the most informative thing the record contains.
`test_the_memo_renders_each_axis_rather_than_a_single_approved_yes` pins it.

**The drug store carries the same coverage / non-coverage / freshness line as
everything else.** `ApprovalAnswer.coverage_lines()` states when the asset was
searched and what openFDA reported, and names what is NOT searched from
`NOT_SEARCHED`: FAERS (20.7M reports), the Orange Book (48,502 records —
therapeutic equivalence, patents, exclusivity) and drug shortages (1,651), plus
the standing caveat that drugsFDA is US applications only. A gap that is
declared is not the same as a gap nobody noticed, and the exclusivity one
matters most: "no ANDA filings matched" is consistent with the molecule still
being under exclusivity but is not proof of it, and the block says so rather
than letting a reader infer it.

**The question set gained `regulatory-status`, phrased as "what does the FDA
record show", not "is it approved".** A yes/no question invites an answer that
treats an empty database as a no — the exact failure everything above exists to
prevent — so the question asks what is on file.
`test_the_shipped_question_set_has_a_question_that_reaches_the_drug_store`
pins that some question actually routes there, because a store no question
reaches is a store no memo uses, which is what this whole change was fixing.

**A drug citation resolves to an application number, not a generic FDA RECORD.**
`context.FDA_DRUG_LABEL` is its own kind with the application number as the
identifier, printed the way the FDA prints it — `NDA 021923`, spaced, not
`NDA021923` — so an analyst can paste it into Drugs@FDA; the stored value keeps
the API's own spelling so a round-trip stays lossless, because `FDA RECORD — K123456` beside `FDA RECORD — BLA125514`
invites a reader to treat substantial equivalence to a predicate and a
demonstration of safety and efficacy as the same kind of fact. Single citation
numbering across all four stores, as always. The router gained
`needs_drug_regulatory` as a second orthogonal flag — both can be true, since
"what is the regulatory status" legitimately asks both — so a device question
does not pay for a drug lookup and a drug question is not answered only from the
device store.

**Premarket APPROVAL is a fourth regulatory fact, and 510(k)-only was hiding
Class III devices entirely.** A 510(k) is clearance by substantial equivalence to
a predicate; a PMA is approval on clinical evidence; a De Novo is granted BECAUSE
no predicate exists. Three pathways, three facts, and `device_answer.py` has no
field spanning them — there is deliberately no `is_cleared_or_approved`.
Measured on 18 real device types, **7 (39%) return zero 510(k) clearances and do
have PMA records**: implantable cardioverter defibrillator (4,330 records / 31
applications), drug-eluting coronary stent (1,399/17), cochlear implant (772/10),
bone growth stimulator (292/13), deep brain stimulator (449/4), HPV test (87/5),
LVAD (38/2). Seven more appear under BOTH pathways, so a 510(k)-only view of them
was real but partial — even this tool's own worked example, the FRN infusion
pump, has 1,148 PMA records across 30 applications it never saw. After the fix,
zero of the 18 are absent from everything.

**Bulk ingestion is shared infrastructure (`fda/bulk.py`), not a PMA special
case.** openFDA caps `skip` at exactly 25,000 (measured: 25,000 returns 200,
25,001 returns HTTP 400) against 56,853 PMA records, so the API reaches at most
44% of the source and can never state a complete denominator. The bulk export
can: one partition, 20.9 MB, `total_records` matching the API exactly.
`load_export` takes an injected `fetch`, so download → unzip → parse →
completeness assertion is fully tested against real captured zip bytes with no
network, and it raises `IncompleteBulkExport` when the parsed count misses the
declared one — the same fail-loudly choice `run_query` makes against
`countTotal`. The Orange Book and Purple Book are bulk distributions too and
will reuse this unchanged.

**Freshness for a bulk source is `export_date`, and refresh means re-download —
stated, never implied.** `BulkFreshness` carries FDA's own export date (what the
data IS) separately from `downloaded_at` (when we took a copy), and
`render_lines()` says outright that the source cannot be refreshed incrementally
and that anything published after the export date is not in this copy. Implying
incremental freshness for a source that has none is the same class of error as
implying a capped sample is a census.

**The decision codes were read from the FDA data dictionary, and the dictionary
and the data disagree.** Every meaning in `config/fda_decision_codes.yaml` is
verbatim from `open.fda.gov/fields/devicepma.yaml` and `deviceclearance.yaml`;
none was inferred from the letters, which was measurably necessary — `APRL`
reads like "approvable letter" and actually means "Reclassification after
approval", and an earlier draft of this work had `SESK` and `SESU` swapped. The
disagreement is not marginal:

```
observed but UNDOCUMENTED   OK30 27,693 (49% of the source!) · APCB 11 · DENG 482
documented but ABSENT       DENY 0 · WTDR 0 · LE30 0 · GT30 0 · SESR 0
```

An undocumented code renders as undocumented and never as an approval.
`PMA_DECISION_UNDOCUMENTED` is its own state precisely so 49% of the source
cannot be quietly folded into either an approval or a denial.

**"Has a PMA record" is not "was approved", the same way "has a drugsFDA
record" was not.** Of the 1,473 original applications: 844 `APPR`, 618
approved-then-changed (`APWD` "Withdrawal after approval", `APRL`
"Reclassification after approval", `APCV` "Conversion after approval" — all of
which mean approval DID happen), and 11 carrying the undocumented `APCB`.
`approval_state` is four values, not a boolean, and `has_pma_approval` requires
positive evidence.

**A PMA is NOT automatically Class III.** Measured across the whole export:
class 3 = 48,473, **class 2 = 7,177**, plus 797 with no class, 236 "U", 162 "f",
8 class 1. Collapsing the pathway into the class would be wrong on 14% of
records, so `device_class` is carried verbatim and the rendered line says it is
read from the record rather than inferred.

**Originals are keyed off `supplement_number`, not `supplement_type` — and
getting that wrong overstates the approval base by 128%.** Supplements are
SEPARATE RECORDS in this source (the opposite of drugsFDA, where submissions
nest), so the key is `(pma_number, supplement_number)`. `supplement_type` is
empty on all 1,473 originals, which makes it look like the discriminator — but
it is ALSO empty on 1,885 genuine supplements, older records such as
`N16993 S007`. Using it counts 3,358 originals where there are 1,473. Caught by
cross-checking both discriminators against the full export rather than trusting
the one that looked right on a 500-record sample. 270 `pma_number`s appear only
as supplements, their original absent from the export; `has_original_record`
carries that gap rather than papering over it.

**`device/pma` has no `device_name`, and assuming symmetry with the 510(k) path
found nothing at all.** `trade_name` and `generic_name` are the equivalents. Two
matcher facts came out of this. First, the tokeniser needed extending, not
forking: `agents.parse_descriptive_name` makes each significant WORD its own
ANDed term, because a device name is a description whose words the registry
reorders freely — the registry writes "Defibrillator, automatic implantable
cardioverter" where a query says "implantable cardioverter defibrillator", and
joined into one token those share nothing. `parse_asset` keeps the joined
reading for drugs, where "trastuzumab deruxtecan" is one molecule. Second, the
first version of the 18-device measurement reported "human papillomavirus test"
as absent from both pathways; that was a matcher artefact, not a regulatory
fact — it has 87 PMA records across 5 applications. The 33% first reported was a
lower bound; the corrected figure is 39%.

**De Novo is flagged, and until now every one of the 482 rendered as a
substantial-equivalence finding.** That is a false statement about a company's
regulatory history: a De Novo is granted precisely because no predicate exists.
`is_de_novo` reads the config, `clearances.is_de_novo` is set on ingest, and the
`FDA DE NOVO AUTHORISATION` evidence kind states the pathway in the context
block. Corrections to what this file previously recorded: DENG is **482**, not
481; **`DECL` does not occur in the data at all** and is not in the 510(k) data
dictionary either, so the earlier note calling it "the paired De Novo declined
code, also worth surfacing" was wrong; and `SESK` (499) and `SESU` (434) are
real documented codes that went unrecorded here.

**FAERS closes the asymmetry — devices had MAUDE, drugs had nothing — and it is
the source most likely to produce a confidently misleading memo.** It yields
large, specific-looking numbers that invite exactly the reading they cannot
support. `faers.py` is written guard-first: the interpretive block renders in
FULL on every section, before any count is broken down, never as a footnote and
never conditional on the numbers being large.

**A count is not a rate, and the guard is in the type as well as the prose.**
FAERS has no denominator — it does not record how many people took the drug — so
`FAERSAnswer` has no field or method whose name contains rate, incidence,
frequency or risk, and nothing divides one count by another.
`test_the_answer_object_exposes_no_rate_and_cannot_compute_one` asserts that by
inspecting `dir()`, so adding one fails the suite rather than shipping.

The five caveats, all rendered every time: no denominator so the counts cannot
say how often an event happens or what share of patients experienced it, and two
drugs' counts cannot be compared; reporting volume is biased by media attention,
litigation and recency of launch; nothing establishes causation and the FDA does
not validate the reported role; reported events include the underlying disease;
openFDA dedupes report VERSIONS but not independent duplicate submissions.

**The wording avoids the nouns it denies — the third time this has bitten.**
An earlier draft said "not rates, not incidences", which contains "incidence",
so the forbidden-phrase check fired on the tool's own disclaimer. Same lesson as
"is unapproved" and "submitted but not approved": the denial must not share a
substring with the claim. It now says "cannot say how often an event happens,
how likely it is, or what share of patients experienced it", which is also
clearer for a non-epidemiologist.

**Two concrete facts do more work than the caveats.** Measured, the top
co-reported event for pembrolizumab is MALIGNANT NEOPLASM PROGRESSION (12,012) —
the cancer progressing, which is the indication, not a drug effect. And
`drugcharacterization` shows 38,654 CONCOMITANT against 104,464 suspect: a third
of the reports counted for that drug record it as merely present alongside the
drug actually suspected. Both are rendered explicitly; the FDA's own field
reference is quoted verbatim ("Reported role of the drug in the adverse event
report. These values are not validated by FDA", and Suspect = "considered by the
reporter to be the cause").

**Retrieval is cached aggregates — not a mirror, not live per memo.** 20.7M
reports and 113 GB rules out mirroring; querying live per memo breaks offline
mode and makes a memo unreproducible. Server-side `count` aggregations give what
a memo needs (report counts, co-reported event frequencies, seriousness,
reporter type, drug role), and each is cached with its retrieval timestamp in
`faers_cache`. The cache IS the mirror: bounded, reproducible, offline-capable.
Measured: a cache hit is 0.25s against roughly 60s live. `--offline` reads the
cache and, when there is none, returns `offline_miss=True` which renders as "no
cached aggregate for <asset>, run once without --offline" — never a silent zero.

**Matching ORs the normalised block with the free-text name, because the
normalised field alone reports zero for investigational assets.** Database-wide
linkage: `patient.drug.medicinalproduct` 100%, `openfda.generic_name` 88.8%,
`openfda.substance_name` 85.7%. Per asset the gap is far worse than that average
suggests — irinotecan returns 12,783 reports on the normalised block against
**47,829** on the free-text name (3.74x), and botensilimab and zanzalintinib
return ZERO normalised with 1 and 36 free-text reports. Matching the normalised
field alone would have reported no adverse-event reports for two investigational
assets, which is a false clean-safety impression produced by a matching
artefact. Both counts stay separately visible, and the FDA's own description of
the free-text field is quoted: "not systematically normalized", "may contain
misspellings". Every count is stated as a LOWER BOUND.

**Undocumented codes are labelled, not printed as bare numbers.**
`drugcharacterization` returns a bucket for code 4, which the FDA data
dictionary does not define (it documents 1, 2 and 3 only). It renders as "code
4, which the FDA data dictionary does not define" — the same treatment as the
undocumented PMA decision codes. `occurcountry` returns HTTP 500 on this
endpoint and is recorded as unavailable rather than retried.

No disproportionality measure (PRR, ROR) is computed. Adding one would need the
same guard again — that it signals rather than demonstrates — and the counts
alone did not need it to be useful.

**The self-contradicting-caveat lint (`phrasing.py`) — three worked examples was
enough.** Three times a caveat written to DENY a claim contained the claim
verbatim: "this is NOT a finding that the asset is unapproved", "submitted but
not approved (or still under review)", "not rates, not incidences". Each was
caught by hand when a forbidden-phrase test fired on the tool's own disclaimer.
`phrasing.audit()` now checks caveat CONSTANTS against `CLAIM_PHRASES` grouped
by domain (approval, clearance, epidemiology, safety, protection), and
`tests/test_phrasing.py` pins all three historical regressions as cases it must
catch — if a future edit stops catching them, that is a silent weakening and the
suite fails. It lints the constants, not rendered text, because rendered text
legitimately quotes a claim in other roles (`_flag_approval_overreach` quotes
the offending phrase back at the reader on purpose).

It earned its keep immediately, in a way worth recording: the Orange Book
shipped with a new `protection` claim group but was NOT wired into the sweep, so
an injected "this is not a finding that the sponsor has no patents" caveat
passed the lint. Caught by deliberately breaking it. The companion test now
DISCOVERS modules under `medrag/fda` that ship caveat-shaped constants and fails
if any is missing from the sweep, rather than checking a hand-written list —
the same vacuous-coverage failure `test_pdf_render.py` guards against.

**The Orange Book's absence problem is the worst in the tool, and it is an
APPLICABILITY problem rather than an absence one.** An investigational asset
cannot appear in the Orange Book: listing requires an approved application. If
that renders as "no patents found" it reads as "this company has no
intellectual property" — a false statement about the single thing a preclinical
company is worth, produced by a lookup that was never applicable.
`ProtectionAnswer.applicable` is False for an asset with no approved
small-molecule application, and the section renders NOT APPLICABLE with the
reason. Three states, not two: not checked, not applicable, and checked. A BLA
asset gets its OWN not-applicable reason pointing at the Purple Book, because a
biologic's absence means something different again.

**The rendered sentence is "earliest listed protection lapses <date>", never
"generics enter <date>".** The first is what the data says; the second depends
on litigation, settlements, first-filer exclusivity and whether anyone chooses
to enter, none of which is recorded. `test_the_section_says_protection_lapses_never_that_generics_enter`
pins it, and `phrasing.CLAIM_PHRASES["protection"]` carries "generics enter" so
the lint catches a caveat that drifts into it.

**The limits are IN the section, not only here.** `LIMITS` renders on every
Orange Book block: these are the patents the sponsor CHOSE TO LIST so it is not
a patent estate; it is not freedom-to-operate; listed dates ignore litigation
and settlements; small molecules only.

**Measured shape (openFDA bulk export, 2026-08-06).** 48,502 records, one per
(application, product_number), bulk and API agreeing exactly. Patents and
exclusivity are SPARSE and the sparsity is the interpretive point: 2,634 records
carry listed patents (5.4%) and 1,192 carry exclusivity (2.5%); 37,651 entries
are generic (ANDA) against 10,851 NDA. So 24% of NDAs carry listed patents at
any one time, and for the rest it usually means the listed patents expired and
were removed rather than that none existed — which the section says rather than
leaving the reader to infer. A generic-only match is handled separately again: a
generic lists no patents of its own, so their absence there is the expected
shape, not a finding. That case was a real gap the tests caught.

One premise correction: **there IS an `api.fda.gov/drug/orangebook.json`
endpoint.** An early probe returning HTTP 000 was a connection failure, not a
404. The bulk export is still the right ingest — 2.33 MB, one partition, a
complete denominator — and it reused `fda/bulk.py` from phase 1 unchanged, which
was the point of building it that way.

**Exclusivity codes are CURATED, not sourced, and labelled as such.** Unlike
`config/fda_decision_codes.yaml`, whose meanings are verbatim FDA text,
openFDA's field reference documents `exclusivity_code` only as "Code to
designate exclusivity granted by the FDA to a drug product" — it does not
enumerate the values, and the FDA's legend is not published in a machine-
readable form. So `config/fda_exclusivity_codes.yaml` asserts meanings for only
the two the question turns on (ODE* orphan, PED paediatric), marks them
`curated: true`, and records the other seven observed codes with NO meaning
asserted. The rendered line says the classification is curated and from where.

**The Purple Book is not an openFDA source, and a 404 nearly recorded it as
nonexistent.** `drug/purplebook` genuinely 404s (probed twice) and it is absent
from `download.json`. The real distribution is a monthly CSV at
`accessdata.fda.gov/drugsatfda_docs/PurpleBook/{year}/purplebook-search-{month}-data-download.csv`.
But `purplebooksearch.fda.gov` answered three consecutive requests with HTTP
**404** and a 420-byte body that was Akamai's bot-detection "apology" page,
triggered by request rate — a browser User-Agent after a pause returned 200 and
47 KB. Reading the status alone would have recorded the source as unavailable,
which is the phase-3 lesson recurring in a new form: a status code is not a
finding. `bulk.check_not_blocked` raises `BlockedByBotDetection` on that body,
and `http_fetch` now sends a browser User-Agent.

**`bulk.py` gained a delimited mode rather than a Purple Book fork.** The
existing `load_export` assumes a catalogue entry, zipped JSON, and a declared
`total_records` to assert against. A monthly CSV has none of those, so
`load_delimited` handles delimited sources — and `BulkFreshness` gained
`completeness_asserted`, False here, which renders as "the publisher declares no
record count for this file, so the row count is what parsed". A catalogued
export and an uncatalogued CSV cannot make the same guarantee, and the coverage
line must not imply they do.

**The file is TWO sections and only the second is the database.** Each monthly
file opens with a changes report (newly approved / added / updated), then
repeats the identical header and lists every product. `section=1` selects the
full database; taking the first would silently reduce the Purple Book to one
month of changes. A `PurpleBookLayoutError` fires if a needed column is renamed,
because a CSV has no schema version and a renamed `License Type` would turn
every biosimilar into an originator, silently.

**Exclusivity coverage is the finding here, and it was measured rather than
assumed.** Phase 3's shape does not transfer:

```
Exclusivity Expiration Date                     0 of 2,205    0.0%
First Interchangeable Exclusivity Exp. Date    34 of 2,205    1.5%
Ref. Product Exclusivity Exp. Date             36 of 2,205    1.6%
Date of First Licensure                        37 of 2,205    1.7%
Patent List Provided                           49 of 2,205    2.2%
Orphan Exclusivity Exp. Date                  564 of 2,205   25.6%
```

The headline exclusivity column is empty on EVERY row. So the section leads on
what the source answers well — reference product, licensed biosimilars,
interchangeability, all at **100% linkage on the 228 biosimilar rows** — and
treats an exclusivity date as an occasional extra that states its own fill rate.
A section shaped like the Orange Book's would print a blank field 98% of the
time and invite a reader to conclude something from the blank. Measured shape:
2,205 product rows, 847 BLAs, 1,977 originator (89.7%), 128 interchangeable
(5.8%), 100 biosimilar (4.5%).

**Three variants of absence, and the third is specific to this source.**
(1) A small-molecule NDA asset is the WRONG BOOK — routed on application type,
and the section says the Orange Book was consulted instead, the mirror of what
phase 3 does for a BLA. (2) An investigational biologic is absent by
construction. (3) **A licensed biologic with no biosimilars listed means no
biosimilar has been LICENSED** — biosimilar development programmes are not
publicly registered and are invisible here until the day they are licensed, so
rendering that as "no biosimilar competition" would be a false statement about
the competitive position of exactly the assets a healthcare investor cares most
about. `NO_BIOSIMILARS_NOTE` is the fixed text; three tests fail if it is
replaced by a competition claim.

**Biosimilar entry is not generic entry, and is rendered in a different shape.**
A biosimilar needs its own clinical programme, is NOT automatically substitutable
unless separately designated interchangeable, and uptake behaves nothing like
generic substitution — so the gap between a listed exclusivity date and actual
competition is wider here than for a small molecule, and the section says so.
**Interchangeability is recorded separately from biosimilarity** — its own
column, its own `Inter. Approval Date`, its own rendered line — because they are
different FDA findings. Live example: adalimumab has 10 licensed biosimilar BLAs
of which 8 carry an interchangeability designation.

**A fourth self-contradicting caveat, caught by the test rather than by hand.**
The empty-exclusivity line read "no exclusivity date is recorded against these
rows" — containing "no exclusivity". Reworded to "the FDA has not published a
date in these fields". The lint from phase 3 is what made this a one-line fix
instead of a fifth worked example.

**FDA recalls and adverse events are a deterministic negative-evidence half,
beside stopped trials but on their own memo lines.** A recall is a database fact,
not a model judgement, so it lives in `run_negative_pass` next to
`find_stopped_trials`; `NegativeEvidence.fda_searched` keeps "no FDA store" distinct
from "no recalls found", the same not-assessed-vs-nothing-found rule. A recall and
a halted trial are different failure modes, so the memo gives each its own heading
rather than merging them. MAUDE is enormous (~1.8M reports for one product code),
so adverse events are always hard-capped and severity-sorted (Death → Injury →
Malfunction), and the memo says "N of M shown" rather than implying it saw them
all. Clearances carry an `FDA RECORD` provenance label in the single assembled
numbering (trials, then FDA, then literature) — no parallel scheme.

**The claim verifier does not transmit before a per-run confirmation.**
`ClaimVerifier.verify` raises `ConfirmationRequired` *before* any store or model
is touched when the provider is remote. Deck-derived claims are more sensitive
than an asset name, so a setting chosen once weeks ago is not consent — the
notice shows exactly what would be sent and to which provider, every run. Ollama,
`none`, and offline mode are local and skip it. Do not "streamline" this into a
remembered flag.

**Validation runs against the assembled `Evidence` list, not raw passages.**
Trials are numbered before literature in the context, so validating against the
literature subset shifted every marker: real citations were flagged invalid and
figures from trial records were flagged ungrounded. Pass `evidence=` to
`validate_answer`, never `retrieved=`, anywhere both stores are in play.

**Default provider is `none`.** A fresh install must not be able to spend money
by accident. A key present with no provider named infers OpenAI, for backwards
compatibility with `OPENAI_API_KEY`.

**JSONL is split on newline only, never `str.splitlines()`.** This is the single
most expensive lesson in the file. A corpus of 170 records read as "169 loaded, 8
unreadable, one truncated mid-string at char 4408", and every subsequent ingest
died at the same offset. Nothing was corrupt. One Cochrane record carried U+2028
LINE SEPARATOR inside its conflict-of-interest statement; `json.dumps` correctly
leaves it unescaped because it is legal inside a JSON string, but Python's
`str.splitlines()` treats it as a line break — along with U+000B, U+000C,
U+001C-U+001E, U+0085 and U+2029. The reader chopped one valid record into eight
pieces and blamed the writer. `jsonl.py` now owns both halves of the rule:
`split_lines`/`iter_lines` split on `\n` alone, and `dumps_line` escapes
U+2028/U+2029 on write so the files are safe for any other reader too. Both the
corpus and `chunks.jsonl` go through it, because the index had the identical
latent defect. If you are about to write `.splitlines()` against stored data,
this is why you should not.

**A malformed corpus line is skipped, counted, and quarantined — never silently
dropped.** `read_corpus` returns a `CorpusHealth` alongside the documents;
unreadable lines are written verbatim to `corpus.jsonl.quarantine.jsonl` beside
the corpus (encrypted with the corpus's own passphrase when there is one, since
they are corpus text). The count reaches `medrag stats`, the Streamlit settings
panel, and the warnings block of any memo built while records are quarantined,
in plain language. Counts are recomputed on every read rather than cached, so a
repaired corpus stops warning and cannot go stale. Tolerance without a count is
worse than the crash it replaces: it is the same class of failure as a memo that
quietly drops thirty trials.

**`save_corpus` appends; it does not read the corpus back.** It used to load the
entire corpus, merge in memory, and rewrite the file on every ingest, which is
why one unreadable line killed all *future* ingests and why the corruption window
grew with the corpus. New records are appended, and known doc_ids live in a
sidecar (`corpus.jsonl.ids`) so dedup never rehydrates a Document. The "one
record per doc_id, latest wins" invariant moved to read time, so it holds whether
the writer appended or rewrote. An encrypted corpus is the exception — AES-GCM
wraps the whole file, so there is nothing to append into and the rewrite path
stays.

**`write_secure`'s temp file is uniquely named.** It was a fixed `<name>.tmp`,
which two concurrent writers shared: they interleaved into one temp file and each
`finally` deleted the other's, so one ingest silently lost its records and the
other died renaming a file that no longer existed. Running the Streamlit app
beside a CLI ingest is enough to hit it. The rename is followed by a directory
fsync so a power loss cannot lose it.

**Fetched abstracts are not thrown away by a local write failure.** A crash after
74 successful fetches discarded all 74. `ingest_pubmed` now parks them in
`corpus.jsonl.pending` and says so in plain language; the next `save_corpus`
absorbs them, so nothing is re-fetched.

**`.env` is written 0600 via atomic rename, and the key is kept out of
`Config.__repr__`.** There are tests asserting the key cannot reach a traceback,
a log record, stdout, the corpus, the index, or the memo. Do not add a `__str__`
or a debug print that undoes this.

## Layout

```
medrag/
  config.py         env-driven config; __repr__ redacts secrets
  providers.py      provider presets + base_url; free tiers and local
  documents.py      Document / Chunk / Retrieved
  jsonl.py          how JSONL is split and written — newline only, U+2028 escaped
  ingest/           pubmed.py (E-utilities), pdf.py, store.py (JSONL corpus:
                    append-only, id sidecar, quarantine + CorpusHealth)
  trials/           client.py (CTgov API v2; exhaustive pagination + countTotal
                    assertion), queries.py (query-set union, provenance,
                    marginal yield, basket caveat), store.py (SQLite + FTS5)
  fda/              client.py (openFDA device 510k/recall/event), store.py;
                    bulk.py (SHARED bulk-export ingestion — download, unzip,
                    parse, export_date freshness; Orange/Purple Book reuse it),
                    pma.py (premarket APPROVAL + De Novo, decision codes from
                    config/fda_decision_codes.yaml), device_answer.py (the
                    deterministic three-pathway device answer), faers.py
                    (adverse-event AGGREGATES, cached, guard-first),
                    orangebook.py (listed patents/exclusivity — an APPLICABILITY
                    answer, not an absence one), purplebook.py (licensed
                    biologics, biosimilars, interchangeability — a monthly CSV,
                    not an openFDA endpoint);
                    drugs.py (drugsFDA/label/enforcement — approval status, label
                    sections, matched on active ingredient via agents.py),
                    drug_store.py (SQLite; ApprovalAnswer, which cannot state an
                    approval from an empty result)
  chunking.py       section-aware chunking; grades evidence + stamps disclosure at ingest
  evidence_grade.py publication type -> study-design tier
  disclosures.py    funder/affiliation/COI signal per document, for the independence axis
  agents.py         shared drug/agent name matching — array parsing, conjunction
                    grammar, generic/brand/development-code aliases
                    (config/agents.yaml). The ONE matcher; drugsFDA reuses it
  phrasing.py       the self-contradicting-caveat lint — a caveat must not
                    contain the claim it denies (three real regressions)
  markers.py        shared marker vocabulary, negation grammar, signal collection —
                    the ONE implementation both biomarker.py and biomarker_gating.py
                    reduce with their own, deliberately different, policies
  biomarker.py      patient-side eligibility match: ELIGIBLE/ELIGIBLE BY EXCLUSION/
                    EXCLUDED/UNCLEAR/NOT MENTIONED, UNCLEAR-on-conflict
  biomarker_gating.py  trial-side census: REQUIRED/ELIGIBLE_BY_EXCLUSION/EXCLUDED/
                    NOT_MENTIONED, REQUIRED-wins-on-conflict
  landscape.py      patient trial landscape — condition + biomarker -> enterable
                    trials, ranked across every admitting state, capped at 30
                    with sample_lines() stating what the cap held back
  ranking.py        deterministic, explainable relevance score for BOTH capped
                    trial tables — the aggregate section's sample and the
                    patient landscape (config/ranking.yaml) — no model call
  coverage.py       the coverage statement — searched/not-searched/what-matched,
                    one render function, three surfaces (config/registries.yaml)
  landscape_memo.py landscape table -> Markdown + PDF (reuses table_render.py)
  table_render.py   shared Markdown + reportlab table renderer (claims + landscape);
                    owns both page-overflow fixes — splitInRow for a row taller
                    than the frame, fit_widths for a budget wider than it
  embeddings.py     OpenAI / sentence-transformers / hashing fallback chain
  vectorstore.py    FAISS IndexFlatIP + NumPy fallback, optional encryption
  retriever.py      MMR re-ranking, per-doc caps, evidence-tier boost
  router.py         STRUCTURED / SEMANTIC / BOTH
  context.py        provenance-labelled evidence assembly
  negative_evidence.py  stopped trials + contradiction hunt
  claims.py         claim verification — five verdicts, deterministic overlays,
                    transmission-confirmation gate (inverse of the memo flow)
  claims_memo.py    claim result table -> Markdown + PDF (memo.py house style)
  diligence.py      question-set runner, memo assembly
  memo.py           Markdown + PDF rendering
  validation.py     citation coverage, marker validity, numeric grounding
  crypto.py         AES-256-GCM at rest, fail-closed guard
  autoload.py       fetch-on-demand so users never touch a terminal
  setup_env.py      reads/writes .env from the app
config/diligence_questions.yaml   THE QUESTION SET — edit this, not code
config/trial_queries.yaml         registry synonym sets — edit this, not code
config/agents.yaml                 drug generic/brand/development-code aliases — edit this, not code
config/fda_decision_codes.yaml     FDA decision codes, VERBATIM from the FDA data dictionary,
                                   with documented/observed recorded — edit this, not code
config/fda_exclusivity_codes.yaml  Orange Book exclusivity codes — CURATED, not FDA-sourced,
                                   and labelled as such — edit this, not code
config/fda_biologic_exclusivity.yaml  Purple Book exclusivity FILL RATES (measured, not
                                   assumed) and the empty-field note — edit this, not code
config/markers.yaml                biomarker vocabulary, negation forms, curation — edit this, not code
config/ranking.yaml                 sample-ranking weights, justified per-signal — edit this, not code
config/registries.yaml              which registries are/aren't searched — edit this, not code
app.py              Streamlit launcher (memo page)
pages/2_Verify_Claims.py          Streamlit claim-verification page
pages/3_Trial_Landscape.py        Streamlit patient trial-landscape page
```

## Conventions

- **An invariant enforced in the library needs a test at the layer that calls
  it.** A guard production bypasses is decoration. `claims.py` raises
  `ConfirmationRequired` before transmitting, and five tests pin it — and it
  never fired, because both Streamlit pages pass `confirmed=True`
  unconditionally and the real decision was an untested checkbox. The tested gate
  and the operative gate were different objects, so the library tests stayed
  green through two live bypasses. When you add a guard, ask who actually calls
  it in production and write the test there. `tests/test_consent_gate.py` is the
  worked example: it drives the real page through `streamlit.testing.AppTest`
  rather than calling the library function the page calls.
- **Tests never touch the network**, and this is enforced, not just documented.
  `tests/netguard.py` blocks outbound sockets; `conftest.py` installs it for
  pytest and every test file installs it on import for direct runs. Loopback is
  allowed so multiprocessing works. External services are driven through mocked
  transports against captured fixtures (`tests/fixtures/`). Escape hatch:
  `MEDRAG_ALLOW_TEST_NETWORK=1`, or `@pytest.mark.allow_network` under pytest —
  there are no uses of either today, and adding one should be argued for.
- **Tests must not depend on untracked local state.** Four consent tests passed
  only because the developer's machine had a `.env`; on a clean checkout the page
  took its "SETUP NEEDED" branch and rendered no widgets. A test that needs a
  file the repo does not ship has to create it. CI catches this now, because CI
  is a fresh clone every time.
- Each test file is runnable directly (`python tests/test_trials.py`) as well as
  under pytest. CI runs both ways, because a convention nothing checks stops
  being true.
- **A test that renders an artefact has to render the shape that breaks it.**
  Three tests built PDFs and all three passed while the landscape PDF was
  crashing on real data, because fixture rows are short and the failure needs a
  row taller than a page. `tests/test_pdf_render.py` feeds every renderer the
  pathological shapes on purpose. The same file shows the second half of the
  rule: a sweep across renderers needs a companion test asserting the sweep
  actually reached each one, or it passes vacuously — which is exactly what it
  was doing for the diligence PDF until it was driven with a census section.
- Test names state the property, not the function: `test_stopped_trials_sort_first`
  rather than `test_query`.
- Comments explain *why*, never *what*. If a comment restates the code, delete it.
- Plain-language errors in anything a non-technical user sees. No tracebacks as
  the primary message; say what to do next.
- Prefer failing closed. `crypto.write_secure` refuses to write plaintext when
  encryption is enabled rather than silently degrading.

## Running it

```bash
pip install -r requirements.txt
python -m medrag doctor                     # connectivity check
python -m medrag ingest --query "..." --index
python -m medrag trials --condition "..." --intervention "..."
python -m medrag fda --product-code "FRN" --device-name "infusion pump"
python -m medrag drugs --asset "pembrolizumab"
python -m medrag pma                        # bulk download of premarket approvals
python -m medrag faers --asset "..."        # cache FAERS aggregate counts
python -m medrag orangebook                 # bulk download of listed patents/exclusivity
python -m medrag purplebook                 # monthly CSV of licensed biologics/biosimilars
python -m medrag diligence --asset "..." --indication "..."
python -m medrag verify --claims claims.txt --asset "..." --company "..."
python -m medrag landscape --condition "..." --biomarker "MSS" [--location "..."]
streamlit run app.py
```

No network? `python scripts/make_sample_corpus.py && python scripts/make_sample_trials.py
&& python -m medrag index` seeds synthetic data. The sample records use
`SAMPLE-*` ids and `[SYNTHETIC]` titles so they can never be mistaken for real
findings.

## Known-unverified — things that have never run for real

Everything below was built and unit-tested against mocks, but the sandbox it was
written in blocked all outbound network. **These need one real run each, and are
the highest-value things to check first.**

1. Live PubMed E-utilities fetch and parse.
2. Live ClinicalTrials.gov API v2 pagination and `whyStopped` fill rate still
   want a real run. The eligibility/contacts/description modules HAVE now been
   fetched for real once (30 colorectal-cancer records): `eligibility_criteria`
   30/30; conditioned on RECRUITING/NOT_YET_RECRUITING (the only statuses the
   landscape surfaces) 3/4 carried a central contact and 3/4 an overall official.
   Two field realities the fixtures now mirror: `location.status` is frequently
   empty on live data, and `central_contacts` is legitimately absent on COMPLETED
   and TERMINATED records (their `contactsLocationsModule` has no `centralContacts`
   key) — so the low mixed-status contact rate is registry sparsity, not a parse
   bug. Per-site `locations[].contacts[]` (present on 58/60 sites of a recruiting
   trial) is now parsed and preferred over the study chair.
   Pagination past one page IS now proven: a live `-c "colorectal cancer" -n 500`
   fetched 500 records across multiple `nextPageToken` pages, `whyStopped` fill
   rate 52/57 (0.912) on the stopped subset, `eligibility_criteria` 253/253 (100%)
   on the "colorectal cancer" match set. Investigator-email fill is status-bound:
   96% (51/53) on RECRUITING/NOT_YET_RECRUITING but 36% across all statuses —
   registry sparsity on completed/terminated records, not a parse bug. Still
   unproven: the fill rate on non-oncology conditions.
   Exhaustive pagination IS now proven end to end (2026-08-03): a full
   `query.cond="colorectal cancer"` fetch walked 11 pages and returned exactly
   10,193 of a reported 10,193 in ~22s, ~21 MB of eligibility text. `countTotal`
   is reliable and pagination terminates cleanly — walked == reported on every
   one of the 14 live queries probed. Also measured: `query.cond` is MeSH-expanded
   server-side (a `"colorectal cancer"` query returns records registered as
   "Colorectal Neoplasms"), and `filter.ids` ANDs with `query.cond`, which makes
   set membership a one-request test rather than a full walk. Still unproven:
   behaviour when the registry updates mid-fetch — which is exactly what
   `IncompleteFetch` exists to catch, and it has never fired for real.
2b. openFDA (`fda/`) HAS now been fetched and parsed live once (product code FRN,
   infusion pumps): the three endpoints' real field shapes drove the parsers and
   the fixtures. Confirmed: `device/event` has no top-level `product_code` (it is
   nested at `device[].device_report_product_code`; a top-level search 404s) and
   uses `YYYYMMDD` dates; recalls carry `product_code` 150/150 and `k_numbers`
   linking to a clearance 134/150; clearances always carry an `applicant` but the
   company behind it is not stable — Imed→Alaris→CareFusion→BD and Hospira→Pfizer
   are the same product line under unrelated names, which is why matching is on
   product code, not company. Still unproven: skip-pagination past the 25k cap,
   and non-device (drug) endpoints.
3. Any real LLM call — OpenAI or a free provider. Prompt behaviour, citation
   discipline, and JSON-mode responses from the router and contradiction hunter
   are all unproven against a real model.
4. `sentence-transformers` local embeddings — the library installs, but the model
   weights never downloaded, so `SentenceTransformerEmbedder` has never
   constructed successfully.
5. The faithfulness validator against real model output. It has only ever seen
   synthetic text. Expect the numeric-grounding check to need calibration — a
   model writing "roughly 30%" for a source saying 28.4% will trip it.
   The claim verifier (`claims.py`) shares that grounding check and inherits the
   same caveat; its verdict classification against a real model is also unproven.
6. Streamlit double-click launch on macOS, and the Gatekeeper dialog.

## Deliberately not built

Reranker fine-tuning, chunking experiments, patent/freedom-to-operate search,
multi-user auth and deployment. These were scoped out to protect a three-week
window, not forgotten.

openFDA device integration is now built (`fda/`), as the third structured store
the earlier note argued for. Only the 510(k) endpoint is ingested; two gaps are
worth naming precisely because they are not the same kind of gap.

**PMA and De Novo are now BUILT.** Both were recorded here as known gaps and
both are closed; the numbers below are measured (2026-08-06), and two of the
earlier notes in this file were wrong.

**THE FDA SURFACE: WHAT IS BUILT AND WHAT IS NOT, COMPLETE.** Recorded here so
absence from this list means "does not exist", not "nobody looked". Counts are
measured (2026-08).

BUILT — device: `510k` clearances (175,686) with the De Novo overlay (482
DENG), `recall`, `event` (MAUDE), `pma` premarket approval (56,853 records,
1,473 originals). BUILT — drug: `drugsfda` approvals (29,252), `label` per
asset, `enforcement` recalls, `event` (FAERS) as cached aggregates over
20,692,690 reports, the Orange Book (48,502) and the Purple Book (2,205).

NOT BUILT, drug:
  * `label` as SEARCHABLE CONTENT. Labels are fetched per asset and capped per
    section; the 261,379-document, 1.76 GB corpus is not indexed, so "which
    drugs carry this warning" cannot be asked. The single largest remaining
    drug gap.
  * `shortages` (1,651 records) — supply, which no other source here covers.
  * `ndc` — the National Drug Code directory, the packaging/marketing layer.

NOT BUILT, device:
  * `classification` — the product-code registry itself, which would let a
    device name resolve to a code without going through clearances.
  * `registrationlisting` (330,251) — who actually makes and markets a device.
  * `udi` / GUDID (5,083,948 device identifiers, 1.79 GB).
  * `covid19serology` (13,420) — a closed historical set.
  * PMA post-approval study status, which is where a conditional approval's
    obligations live.

NOT BUILT, other openFDA domains: `other/unii`, `other/nsde`,
`other/substance`, and **`transparency/crl` — Complete Response Letters**, the
closest public source to an FDA REJECTION. Every regulatory source in this tool
records what was approved, cleared or licensed; none records what was refused,
which is a structural bias in the picture the tool paints. Arguably the highest-
value gap remaining. Food, tobacco, cosmetic and animal domains are out of
scope for this tool entirely.

Also not built: UDI/GUDID.

The openFDA DRUG endpoints ARE now built (`fda/drugs.py`, `fda/drug_store.py`) —
drugsFDA approvals, SPL labels and drug enforcement. Still not built, and
declared rather than left to be inferred: FAERS (`drug/event`, 20.7M reports,
113 GB), the Orange Book (48,502 records — therapeutic equivalence, patents and
exclusivity, and the obvious next addition since it answers "when does this go
generic"), and drug shortages (1,651 records).

## The one file worth changing first

`config/diligence_questions.yaml`. The current question set is a **draft** and
says so. Rewriting it with someone who does diligence for a living is the single
highest-value change available to this project.

---

# Device-parity decisions, recorded 13 August 2026

These are held here rather than only in a session transcript, for the reason
`docs/SCOPE.md` exists at all: a decision that lives in a transcript is not
held. Each states what was decided, what it was measured against, and what
would have to change to revisit it.

## 1. A device-role axis is approved in principle and is NOT built

`intervention_types` stays exactly what the registry states — "what the sponsor
coded" — forever. It is never inferred, never overwritten, and never widened to
absorb trials the registry did not code that way.

The pressure to widen it was real and was measured: a `DEVICE`-only reading
misses 29.2% of PROCEDURE-only trials in device-oriented query sets (n=383,
ranging 8.8% in sleep_apnoea to 45.0% in cardiac_arrhythmia_monitoring), and
between 11% and 33% of zero-intervention observational trials (n=367; the range
is wide because the lower bound is a classifier that demonstrably misfiles and
the upper bound is "mentions a device anywhere"). Absorbing those into `DEVICE`
would make the column assert something the registry never said, which destroys
the one property that makes it worth having.

So any device-role axis is:

  * SEPARATE from `intervention_types`, never merged into it;
  * DERIVED, and labelled as derived wherever it appears;
  * PRINTED SEPARATELY in a memo — never combined into a single
    "device trials: N", because a reader who cannot tell which number is the
    registry's and which is ours has lost the distinction at the point of use;
  * gated on validation against a hand-built ground truth per modality, the way
    the six-trial MSS list validated the biomarker path. Built and unvalidated
    is worse than absent here, because it prints as a count.

States, when it is built: `EVALUATED` (the device is the object of study),
`INSTRUMENT` (a device measures something else), `ABSENT`, `INDETERMINATE`.

## 2. `outcomesModule` alone was tested as the source and REJECTED

Measured 13 August 2026. Recorded so nobody re-runs this experiment in six
months and gets the same answer at the same cost.

Primary-outcome text was fetched for 749 trials (383 PROCEDURE-only + 367
zero-intervention, one overlapping) and scored against 135 HAND-READ trials —
all 55 records where an accuracy/device-evaluation vocabulary fired, and 80 of
the 633 where it did not.

```
precision on EVALUATED     38/55  = 69%
false-negative rate         6/80  = 7.5% of signal-negatives were EVALUATED
implied recall                    ~45%
no primary outcome stated  61/749 = 8.1%
```

69% precision means a third of what such a column would assert is wrong, and it
would print as a count. Rejected on that basis, not on cost — the fetch is
field-limited and took 4 seconds for 749 records, so a full-store backfill is
~20 minutes.

Worth keeping: it is the best SINGLE signal found, and both its failure modes
are structured rather than random (below), so a COMBINED signal is a reasonable
next experiment. That is a different experiment with a different ground truth,
not a tweak to this one.

## 3. The two structured failure modes — the input to the next experiment

**False positives: "sensitivity" used physiologically.** Six of the 17 false
positives were *contrast sensitivity* (x3), *corneal sensitivity* (x2) and
*baroreflex sensitivity* — a physiological measure, not a diagnostic metric.
Two more were "level of agreement" in a Delphi consensus and "strongly
disagree" in a Likert scale.

**False negatives: the outcome states the clinical target, the device is named
only in the title.** All six missed trials had this shape:

```
NCT00800397  "Evaluation of the Noga System"        PO: Detection of Cheyne Stokes respiration
NCT01415037  "Annular Array Ultrasound"             PO: detection of PVD: 20MHz annular array versus 10MHz
NCT03693092  "LAmbre LAAC System Follow-Up"         PO: complications related to the device   <- missed on word ORDER
NCT00797524  "Retinal Leakage Analyzer"             PO: Retinal leakage and retinal thickness
NCT05908188  mandibular movement sensor             PO: ...recorded by the sensor in comparison with a...
NCT02898090  "Development and Validation of ..."     PO: prevalence of appropriateness
```

The worked pair, both verified against the registry rather than transcribed:
`NCT06232174` (*Value of Transcutaneous Bilirubin Devices*) states
"diagnostic accuracy of transcutaneous bilirubin devices" and IS caught;
`NCT04354506` (*Smart Phone Atrial Fibrillation Application*) states "Atrial
fibrillation recurrence" and is NOT. One right, one wrong, and the wrong one
fails in the way that matters most.

## 4. Conditions set in advance for the combined-signal experiment

Set now so the bar cannot move to meet the result.

  * **Pre-registered threshold: precision >= 90% on `EVALUATED`.** Coverage is
    free to be low and is reported as whatever it is. This follows from this
    codebase's own discipline: a classifier that labels 40% and says
    `INDETERMINATE` for the rest is honest; one that labels everything at 69%
    is not. Low recall is a stated limit; low precision is a wrong answer
    wearing a number.
  * **`INDETERMINATE` is the DEFAULT, not the residual.** Anything the signal
    does not clear the bar on stays `INDETERMINATE` and is counted.
  * **A fresh held-out hand-read set, stratified by modality, read BEFORE the
    classifier is written.** The two failure patterns above were derived from
    the existing 135 trials; fixing "contrast sensitivity" and then grading on
    those same 135 is fitting to the test set and would produce a number that
    looks good and does not hold.

If the combined signal cannot clear 90% at any useful coverage, the axis does
not ship, and that goes in `CAPABILITIES.md` as a stated limit rather than
being quietly retried.

## 5. A measurement discipline that cost real work to learn

The parity audit identified device trials with a regex over intervention NAMES,
because the registry's own type field was being discarded. Measured against the
type field once it was stored, that regex recovered roughly **21%** of the real
device population and **55.3%** of what it returned was a drug or a procedure.
Every device-side number in that audit was measured through it.

So: a number derived from a proxy is quoted with the proxy named, and is
re-measured against the authoritative field the moment one exists. The
audit's per-modality gate-rate bands (imaging 3.3%, monitoring 0.7%, implant
0.3%, surgical 1.2%, IVD 5.3%) were all measured through that regex and must be
re-measured against `intervention_types` before being quoted again.
