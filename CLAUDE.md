# CLAUDE.md — working notes for Claude Code

Read this before changing anything. It states the invariants that look wrong
until you know why, and the conventions the suite depends on.

**The reasoning, measurements and history behind every line here are in
[`docs/RATIONALE.md`](docs/RATIONALE.md) — verbatim, by section.** Read a
section there before overturning something, not before an ordinary edit.
A few invariants are NOT here: `.claude/rules/*.md` holds ones that can only be
violated from inside a specific set of files, and they load when you open one of
those files. Anything that warns about a consequence somewhere else stayed in
this file, however subsystem-specific it reads — a rule that fires only when you
open the right file is silent exactly when someone approaches from the wrong
direction. Current scoped rules: `fda-source-shapes.md`, `marker-grammar.md`,
`ranking-weights.md`.

Maintained figures are in `docs/CAPABILITIES.md`; scope in `docs/SCOPE.md`;
`docs/DECISIONS.md` points back here. Numbers in `RATIONALE.md` were measured on
the store of the day and are **not maintained baselines** — re-measure before
quoting one.

## What this is

A diligence tool for biomedical assets, used at a healthcare VC. Given an asset
and an indication it runs a fixed question set against structured stores
(ClinicalTrials.gov, openFDA) and a literature index (PubMed), and produces a
Markdown + PDF memo where every claim carries a PMID, NCT or FDA identifier.

It is not a chatbot. The fixed question set is the point: the same questions in
the same order against every asset is what makes two memos comparable, which is
what makes the output usable in an investment memo.

Primary user: analysts and interns who will not open a terminal. The Streamlit
app is a launcher that produces a PDF, not a place to read findings.

## The rule everything else applies

**"Not assessed", "nothing found" and "found against" are three different
states, everywhere, and absence is never rendered as a negative finding.**
`ValidationReport.assessed`, `NegativeEvidence.searched`/`fda_searched`,
`CoverageStatement.ever_ingested`, `TrialAnchor.notes()`, `LoadReport.read_only`,
`ApprovalAnswer`/`ProtectionAnswer` applicability and FAERS `offline_miss` are
one rule at different layers. It has been a real bug more than once (RATIONALE
§1, §5, §15).

Corollary, which has bitten four times: **a caveat must not contain the claim it
denies** — "this is NOT a finding that the asset is unapproved" contains "is
unapproved". `phrasing.audit()` lints the caveat constants; every historical
regression is pinned in `tests/test_phrasing.py`.

## Decisions that must not be quietly reversed

### Stores and routing — RATIONALE §1

- **Trial records are NOT in the vector index.** Phase and status are filters,
  not semantics. Trials in SQLite, literature in FAISS, `router.py` decides.
  → `test_the_index_build_path_never_embeds_a_trial_record`
- **Bad router output falls back to the RULES, never to BOTH.** Always-BOTH
  looks like it works while doubling cost and diluting every answer.
  → `test_router_falls_back_on_bad_model_output`
- **The negative-evidence model half is explicitly permitted to return an empty
  findings list.** Do not remove that permission: an invented contradiction is
  worse than silence. The other half (`store.stopped_trials`) is pure SQL.
  → `test_the_contradiction_prompt_still_permits_an_empty_findings_list`
- **Stopped-trial lookup ORs intervention and indication, never ANDs them.** A
  compound stopped in a *different* indication is the higher-value finding.
  → `test_stopped_trial_in_other_indication_is_not_hidden`
- **The two stopped arms have separate budgets and denominators, no spillover**
  — their pools are not comparable. Within an arm, order by start date
  descending. → `test_which_rows_are_shown_does_not_depend_on_the_candidate_window`
- **FDA recalls and adverse events get their own memo headings**, hard-capped
  and severity-sorted, saying "N of M shown". A recall and a halted trial are
  different failure modes.

### Claim verification — RATIONALE §2

- **Support and independence are orthogonal axes and must never re-merge.** The
  old single `SUPPORTED - COMPANY SOURCE` hid an independent partial behind a
  scary label. Both are first-class columns in table, Markdown and PDF.
  → `test_the_support_and_independence_vocabularies_never_re_merge`
- **Absence of a disclosure is never independence.** INDEPENDENT needs *positive*
  evidence; everything else is NO DISCLOSURE, a mix reports its counts. The
  model judges only support — independence is computed in code.
  → `test_no_disclosure_is_the_honest_default`,
  `test_independent_requires_positive_evidence`
- **The company link is judged from a document-level disclosure, not the cited
  chunk** — "Funded by X" lives in a different chunk from the result.
  → `test_disclosure_blob_catches_company_when_cited_chunk_does_not`
- **An empty retrieval is NOT FOUND deterministically** — the model is never
  consulted, so it cannot turn "nothing retrieved" into "evidence against".
  UNVERIFIED is a third state. → `test_empty_retrieval_is_not_found_without_calling_the_model`,
  `test_not_found_never_becomes_contradicted`
- **NOT VERIFIABLE is decided at extraction and never silently discarded** —
  otherwise unfalsifiable claims all return NOT FOUND and drown the rest.
  → `test_not_verifiable_verdict_is_recorded_not_dropped`
- **The numeric downgrade is a deterministic overlay, not a model output.**
  → `test_numeric_mismatch_downgrades_to_partial`
- **The claim verifier does not transmit before a per-run confirmation.** A
  setting chosen weeks ago is not consent for deck-derived claims; do not
  "streamline" it into a remembered flag. → `tests/test_consent_gate.py`
- **Validate against the assembled `Evidence` list, never raw passages** —
  trials are numbered before literature, so the literature subset shifts every
  marker. Pass `evidence=`, never `retrieved=`.
  → `test_citations_use_the_assembled_evidence_numbering`

### Biomarkers and markers — RATIONALE §3, §6

- **Biomarker matching never silently drops a trial and always shows its
  criterion sentence.** Five states; every admitting one stays in the landscape
  flagged, and EXCLUDED / NOT MENTIONED are *counted*, never hidden.
- **ELIGIBLE BY EXCLUSION is its own state.** A trial naming MSI-H only to
  exclude it is confident evidence of MSS eligibility, not uncertainty. UNCLEAR
  is reserved for an actual contradiction in the source text.
- **`markers.py` owns the vocabulary and negation grammar; the
  conflict-resolution POLICY is deliberately not shared.** The census has
  REQUIRED win a conflict, the patient side resolves to UNCLEAR. Neither is
  "more correct"; opposite conclusions on ordinary text would be the bug.
  → `test_the_two_modules_never_reach_opposite_conclusions`
- **`resolve_marker` matches the marker the user typed, or nothing.** Exact
  equality against key or alias; no substring fallback, no silent substitution —
  the substring version resolved "MSI-H" onto MSS, the opposite marker, on a
  patient-facing page.
  → `test_no_query_resolves_to_a_marker_that_does_not_list_it_exactly`
- **A curated verdict and an uncurated guess must never look equally
  confident.** An unmatched biomarker falls to `_literal_match` and can only
  return UNCLEAR or NOT MENTIONED; `BiomarkerMatch.curated`/`MarkerFlag` carry
  that to every surface.
- **`config/markers.yaml` owns the marker table** — vocabulary is a clinical
  judgement, never model-generated at query time.
- **The biomarker census is a DERIVED column, so a matcher change forces a
  schema bump** — otherwise counts reflect the old rules and the live screen the
  new ones.
- **The census prefilter is safe only because the census has no UNCLEAR**: a
  conflict resolves to REQUIRED, so a self-contradictory trial always reaches
  the live screen that flags it. → `test_a_self_contradicting_trial_is_admitted_by_both_paths`;
  run `scripts/check_census_parity.py` before changing either matcher.
- **Under the prefilter, `n_excluded`/`n_not_mentioned`/`n_no_eligibility_text`
  come from SQL** — those records are deliberately never loaded, and inferring
  them reports "0 excluded" for a population where hundreds are.
- **A biomarker filter ORs statuses for one marker; different markers still
  AND.** ANDing statuses is unsatisfiable and silently zeroes the section.

### Ranking and capped samples — RATIONALE §4

- **Which trials print is a deterministic, explainable score — no model call
  anywhere in the path**, and `Ranking.explain()` prints which signals fired.
  Ties break on NCT ID: anything that decided a row's position must appear in
  `explain()`.
- **Biomarker state is a labelled column, not a sort key.** Grouping by state
  cancelled the ranking and buried central Phase 3 trials for no reason but
  their sponsors' grammar.
- **The 30-row cap lives in `build_landscape`, not in each renderer, and
  `sample_lines()` states what it held back, BY STATE.** All three surfaces call
  that one function. → `test_the_default_cap_matches_the_diligence_memos_sample_cap`
- `limit` (SCREENED) and `show_limit` (PRINTED) are different caps on purpose.
  The k=30 cap and the `mss-required` status filter are editorial calls for
  whoever owns the config — RATIONALE §4 has what they currently exclude.
- **`phase` is stored POST-conversion** (`"EARLY_PHASE1"` → `"EARLY_Phase 1"`).
  A config written against raw API tokens matches nothing and falls silently to
  the zero-point default.

### Coverage and ingest completeness — RATIONALE §5

- **The trial fetch runs to exhaustion and asserts against `countTotal`.**
  `--max-records` is a testing override with no default; `run_query` raises
  `IncompleteFetch` on a short walk. A cap silently redefines the population as
  "whatever the API returned first".
- **The ingest writes an IN_PROGRESS marker before the first network call** —
  every other guard fires on a RESPONSE, and a killed process raises nothing.
  Three states, and only `record_coverage` clears it; `held` is COUNTED from the
  database, never passed in.
- **`verify_ingest` is the ONE implementation of "did this finish".**
  `CoverageReport.complete` is the WEAK check and says so: it sees errors only.
- **A pre-v9 row grades COMPLETE only when its own numbers prove it; ambiguous
  grades PARTIAL.** A family wrongly told to re-run costs one fetch.
  → `tests/test_ingest_resume.py`
- **`coverage.render_lines()` is the ONLY function that renders a
  `CoverageStatement`**, called verbatim by all three surfaces. Branch order is
  load-bearing — a named failing query prints before the generic partial line —
  and every incomplete branch ends at `_remedy` with the command that closes it.
- **The first coverage line asserts completeness, not a fraction.** There is no
  registry-wide denominator for a query SET; summing overlapping queries
  inflates it materially.
- **The breakdown line is scored against the SECTION's own population**, via a
  `base_clause` captured before the biomarker filter is appended — reusing
  `by_biomarker` always reads zero for the filtered marker.
  → `test_section_narrowed_by_status_gets_a_breakdown_scoped_to_its_own_population`
- **`ELIGIBLE_BY_EXCLUSION` is not a third value of the explicit/synonym
  split** — own-marker-named versus opposite-excluded is a different axis, and
  folding them loses one.
- **"Not searched" is a static fact from `config/registries.yaml`**, rendered as
  "this tool has not looked", never as "0 of 0".

### Population selection — RATIONALE §9, §11

- **The fetch defines the population; the local layer never re-narrows on the
  condition string.** All consumers select by `query_set`, a structured fact
  stamped at fetch time — a substring runs different logic from the fetch and
  discards trials the ingest deliberately retrieved. Local filtering is on
  structured facts only.
  → `test_claim_retrieval_selects_the_fetched_population_not_a_condition_substring`
- **`config/trial_queries.yaml` owns the synonym sets** — a list that changes
  run to run makes two ingests incomparable. Provenance and set membership are
  MERGED on re-ingest, never replaced.
- **Basket trials are only partly reachable, and the gap is stated
  (`queries.BASKET_CAVEAT`), never inferred from absence.**
- **Query-set membership is an indexed join table and the token column was
  DELETED, not kept beside it** — two sources of one truth is exactly how
  `biomarker.py` and `biomarker_gating.py` drifted apart.
- **A query must be ABOUT something** (`trials/anchors.py`): a `store.query`
  with nothing to filter on degrades to `SELECT * FROM trials LIMIT k`, and the
  free-text fallback searches the anchors only and re-checks every row against
  `agents.py`. A loose retrieval is allowed only with a strict check behind it.
  → `tests/test_retrieval_relevance.py`
- **Known and NOT fixed: the free-text fallback fires only on an EMPTY result**,
  so a partial drop (6 of 214) is undetectable. The fix is a matched-total
  return.

### Drug and agent name matching — RATIONALE §10

- **`agents.py` is the ONE matcher and knows nothing about trials** — drugsFDA
  reuses it. Two independently-maintained matchers is the drift that produced
  the biomarker bug.
- **Matching is token set membership, never `LIKE '%asset%'` over a JSON array**
  — the array separator sits between a combination's agents, so the substring
  returned zero and fell through to free-text silently.
- **Aliases expand at QUERY time, deliberately unlike the biomarker census**, so
  a vocabulary edit takes effect against the database already on disk.
- **Regimens are never expanded into component drugs.** Aliases are ORed, so
  listing oxaliplatin under FOLFOX returns every oxaliplatin trial.
  → `test_a_regimen_is_never_expanded_into_its_component_drugs`
- **AND for selection, OR for the negative sweep** — one matcher, two policies.
- **ANDing introduces a new way to return zero, so the collapse names itself**
  (`collapsed_combination_notes`). "Every agent exists but the pair does not"
  produces NO note: that is a real finding.
- **No fuzzy or edit-distance matching.** Drug names differ by one or two
  characters routinely; inventing matches is worse than missing a typo.
- **A device name is a description, not a molecule.** `parse_descriptive_name`
  ANDs each significant WORD because the registry reorders them freely
  ("Defibrillator, automatic implantable cardioverter"); `parse_asset` keeps the
  joined reading, where "trastuzumab deruxtecan" is one molecule.
- **A schema bump whose column is DERIVABLE gets a backfill, not a re-fetch.**
  `_BACKFILLABLE_FROM` lists only the versions where that is honest.

### Retrieval relevance — RATIONALE §12

- **THE FLOOR IS A CHOSEN TRADEOFF, NOT A SEPARATOR.** The on-topic and
  off-topic distributions OVERLAP, so `score_floor` decides which error to make
  and favours an empty section over a false one. Re-measure and read the
  distributions before moving it. `tests/test_retrieval_relevance.py` asserts
  the overlap itself, so a clean separation fails the suite rather than being
  silently inherited.
- **A question interpolating neither `{asset}` nor `{indication}` is a constant
  string** — one fixed vector, one identical retrieval, and nothing detects it
  from outside. → `test_no_shipped_question_is_a_constant_string`
- **`memo.warnings` is re-read AFTER the sections run.** Snapshotted before,
  every warning a section raised reached no reader at all.

### Retry — RATIONALE §8

- **Retried: 429, 500, 502, 503, 504, timeouts, dropped connections. Never a 400
  or 404 — those are ANSWERS.**
  → `test_the_retryable_set_is_transient_failures_only`
- **Backoff floors are seconds, not milliseconds, and jitter is only ever
  ADDED** — sub-second retry is what a scraper does.
  → `test_the_first_backoff_is_seconds_not_milliseconds`
- **An exhausted retry raises the ORIGINAL exception and the family records
  PARTIAL.** A valid-looking empty response would record as a successful query
  with zero results.
  → `test_a_retry_that_exhausts_still_raises_rather_than_returning_empty`
- **Retrying is COUNTED and reported on success as well as failure** — retry is
  precisely a mechanism for turning a loud failure into a quiet delay.
  `RetryBudget` exposes NO field meaning "complete" or "succeeded": completeness
  is `verify_ingest`'s call, in one place. → the `dir()` test in `tests/test_retry.py`

### Read-only serving and the public surface — RATIONALE §7

- **`dbopen.py` is the one place a store is opened for reading**, and a read
  path performs no mkdir, schema write, version write, commit or chmod.
  `refuse_write` raises `ReadOnlyStoreError` at the top of every writer.
- **A missing file RAISES rather than being created** — an auto-created empty
  store answers every question "nothing found" for a question nobody searched.
- **Two read modes, deliberately not one flag.** `mode=ro` keeps locking and
  sees a writer's commits; `immutable=1` is the deployer's explicit assertion
  that the file is frozen.
- **`PRAGMA journal_mode = WAL` on the writable path**, plus a 10s
  `busy_timeout` — without it a polling reader starves an ingest indefinitely. A
  snapshot must be checkpointed before serving (RUNBOOK).
  → `test_the_writable_path_uses_wal_so_the_test_above_cannot_silently_regress`
- **`MEDRAG_READ_ONLY=1` is strictly stronger than offline and implies it**,
  never inferred from whether the mount happens to be writable.
  → `test_read_only_implies_offline_and_drops_the_api_key`
- **No fetch-on-miss reachable from a read path**, and `cfg.read_only` is
  checked FIRST — before any store is opened and **before `force`**.
- **Staleness is recomputed per request, not frozen at import**, and `/healthz`
  answers 503 when currently stale. The masthead and `/healthz` read the same
  embedded `snapshot_meta`, never the file mtime, which dates the copy rather
  than the data.
- **`reqlog.log_exception` builds its line from structured frames** — type plus
  `file:line in function`, never `str(exc)` and never a local, because that
  string routinely quotes the visitor's input. Registered as an
  `@app.exception_handler`, not in the middleware, which sits outside the
  exception.
- **`describe_failure` builds its message from the status code and provider
  name**, never by rendering the SDK exception, which prints the response body.
- **STILL UNSAFE ON A PUBLIC SITE** (also in `docs/RUNBOOK.md`): the Settings
  key button rewrites `.env` for every visitor; memo/PDF exports collide on a
  user-derived filename in `out/`; `app.py` sends question text to the provider
  with no consent gate.

### Providers — RATIONALE §13

- **A CONFIGURED provider that refuses is not the same as no provider.**
  `call_chat` returns `(response, failure)` rather than raising; 400/401/403/404
  latch the model off for the run, 429/5xx/timeouts do not, and anything
  unrecognised is non-fatal because the fallback is extractive evidence rather
  than silence.
- **Default provider is `none`** — a fresh install must not be able to spend
  money by accident. → `test_default_config_costs_nothing`

### Rendering — RATIONALE §14

- **A stale database or index is REFUSED with a rebuild instruction**, not read
  with columns silently missing (`STORE_VERSION`, `vectorstore.INDEX_SCHEMA`).
- **The claims and landscape tables share one renderer** (`table_render.py`), so
  they cannot drift.
- **`table_render.py` owns both PDF overflow modes**, once, for all three
  renderers: `splitInRow` for a row taller than the frame, and a width budget
  scaled to `available_width` for the failure that does NOT raise — reportlab
  draws an over-wide table off the paper and loses the last column. Scaling is
  the backstop, not the plan.
  → `test_no_renderer_builds_a_table_wider_than_its_own_page`
- **Truncating an eligibility criterion was rejected** — the criterion sentence
  is the evidence the row exists to show.

### openFDA — RATIONALE §15

- **Ingredient matching is `agents.py` extended, not forked**, with salt bases
  ADDED as an alternative reading, never stripped — "POTASSIUM CHLORIDE" IS the
  drug.
- **Tentative Approval is NOT approval, and a SUPPL row can never approve an
  unapproved application.** `marketing_status` is a fourth, orthogonal axis:
  approved-then-withdrawn is not never-approved.
- **"Not found in drugsFDA" never renders as "not approved", enforced at the
  type level.** `ApprovalAnswer` has no boolean called `approved`; `is_approved`
  needs positive evidence and is False both when nothing matched and when
  nothing was searched. `ABSENCE_MEANINGS` states the four things absence means.
  → `test_no_renderer_turns_an_empty_result_into_an_approval_claim`
- **The approval statement is RENDERED, not written.** Every guard around that
  object is in CODE, and a model asked to summarise it converts "no application
  matched" into "not approved in the US" in one paraphrase. Three layers:
  `APPROVAL_PROMPT_GUARD`, `_flag_approval_overreach`, and
  `test_a_memo_for_an_investigational_asset_never_implies_non_approval` — whose
  forbidden-phrase list is deliberately duplicated rather than imported, since a
  test importing the implementation's list keeps passing when someone shortens it.
- **Approval is four axes, not a boolean**, each rendered separately.
  → `test_the_memo_renders_each_axis_rather_than_a_single_approved_yes`
- **The question asks "what does the FDA record show", never "is it approved"**
  — a yes/no question invites treating an empty database as a no.
  → `test_the_shipped_question_set_has_a_question_that_reaches_the_drug_store`
- **A drug citation resolves to an application number** (`NDA 021923`, spaced),
  its own evidence kind — a shared `FDA RECORD` label invites reading
  substantial equivalence and a safety-and-efficacy demonstration as one kind of
  fact. Single citation numbering across all four stores.
- **Three device pathways are three different facts**, and `device_answer.py`
  deliberately has no `is_cleared_or_approved`. A De Novo is granted BECAUSE no
  predicate exists, so rendering one as substantial equivalence is a false
  statement about a company's regulatory history.
- **"Has a PMA record" is not "was approved"** — `approval_state` is four
  values, and `has_pma_approval` requires positive evidence.
- **An undocumented decision code renders as undocumented, never as an
  approval** — 49% of the PMA source carries one.
- **`fda/bulk.py` is shared infrastructure** and raises `IncompleteBulkExport`
  when the parsed count misses the declared one. `BulkFreshness` separates the
  publisher's `export_date` from `downloaded_at` and says the source cannot be
  refreshed incrementally; `completeness_asserted=False` for an uncatalogued CSV.
- **A status code is not a finding.** `accessdata.fda.gov` answers rate-limited
  requests with HTTP 404 and a bot-detection body; `check_not_blocked` raises
  rather than recording the source as nonexistent.
- **A licensed biologic with no biosimilars listed means none has been
  LICENSED**, not that there is no biosimilar competition — programmes are
  invisible until licensure. `NO_BIOSIMILARS_NOTE` is the fixed text.
  Interchangeability is recorded separately from biosimilarity, and biosimilar
  entry is not generic entry.
- **The Orange Book is an APPLICABILITY answer, not an absence one.** An
  investigational asset cannot be listed, and "no patents found" reads as "this
  company has no IP". Three states: not checked, not applicable, checked — and a
  BLA asset gets its own reason pointing at the Purple Book.
- **The sentence is "earliest listed protection lapses <date>", never "generics
  enter <date>".** → `test_the_section_says_protection_lapses_never_that_generics_enter`
- **`LIMITS` renders inside the Orange Book block**, not only here: listed
  patents are not a patent estate, not freedom-to-operate, and ignore litigation.
- **FAERS is guard-first: a count is not a rate, in the type as well as the
  prose.** `FAERSAnswer` has no field or method named rate, incidence, frequency
  or risk, and nothing divides one count by another.
  → `test_the_answer_object_exposes_no_rate_and_cannot_compute_one`
- **The five FAERS caveats render in FULL on every section**, before any
  breakdown, never as a footnote and never conditional on the numbers. Every
  count is a LOWER BOUND, and matching ORs the normalised block with the
  free-text name — the normalised field alone reports ZERO for investigational
  assets, a false clean-safety impression from a matching artefact.
- **FAERS is cached aggregates — not a mirror, not live per memo.** `--offline`
  with no cache returns `offline_miss`, never a silent zero. No
  disproportionality measure (PRR, ROR) is computed.
- **Every clearance count carries two statements**: the sample against the
  API-reported total, and the applicant over-count caveat — distinct applicant
  names over-count distinct companies, so a consolidated market reads as
  fragmented.

### Storage, corpus and secrets — RATIONALE §17

- **JSONL is split on newline only, never `str.splitlines()`**, which also
  breaks on U+2028 and six other characters that are legal inside a JSON string.
  `jsonl.py` owns both halves: split on `\n` alone, escape U+2028/U+2029 on
  write. → `test_the_stored_data_readers_never_call_str_splitlines`
- **A malformed corpus line is skipped, COUNTED and quarantined**, with counts
  recomputed every read. Tolerance without a count is worse than the crash it
  replaces.
- **`save_corpus` appends and does not read the corpus back**, with doc_ids in a
  sidecar; "one record per doc_id, latest wins" lives at read time. Encrypted
  corpora still rewrite.
- **`write_secure`'s temp file is uniquely named**, plus a directory fsync — a
  fixed `<name>.tmp` let two concurrent writers delete each other's.
- **Fetched abstracts survive a local write failure**, parked in
  `corpus.jsonl.pending` and absorbed by the next `save_corpus`.
- **`.env` is written 0600 via atomic rename and the key is kept out of
  `Config.__repr__`.** Do not add a `__str__` or debug print that undoes this.
  → `tests/test_privacy.py`

### The device-role axis — RATIONALE §22

- **`intervention_types` stays exactly what the registry states, forever** —
  never inferred, overwritten, or widened to absorb trials coded otherwise.
- **Any device-role axis is SEPARATE, labelled DERIVED, printed SEPARATELY, and
  gated on a hand-built ground truth per modality.** Built and unvalidated is
  worse than absent, because it prints as a count. States: `EVALUATED` /
  `INSTRUMENT` / `ABSENT` / `INDETERMINATE`, the last the DEFAULT rather than
  the residual.
- **Pre-registered bar: precision ≥ 90% on `EVALUATED`**, coverage reported as
  whatever it is, graded on a fresh held-out set read BEFORE the classifier is
  written. `outcomesModule` alone was tested and REJECTED at 69%. If a combined
  signal cannot clear the bar, the axis does not ship and that goes in
  `CAPABILITIES.md` as a stated limit.
- **A number derived from a proxy is quoted with the proxy named**, and
  re-measured against the authoritative field the moment one exists.

## Conventions

- **An invariant enforced in the library needs a test at the layer that calls
  it.** A guard production bypasses is decoration: `ConfirmationRequired` had
  five passing tests while both pages passed `confirmed=True` unconditionally.
  `tests/test_consent_gate.py` drives the real page through `AppTest`.
- **A test that renders an artefact must render the shape that breaks it**, and
  a sweep across renderers needs a companion test asserting the sweep reached
  each one, or it passes vacuously. `tests/test_pdf_render.py` shows both halves.
- **Tests never touch the network** — `tests/netguard.py`, installed by
  `conftest.py` and by every test file on import. Loopback is allowed. The
  escape hatches have no uses today and adding one should be argued for.
- **Tests must not depend on untracked local state.** A test needing a file the
  repo does not ship has to create it.
- Each test file runs directly (`python tests/test_trials.py`) as well as under
  pytest. CI runs both ways.
- Test names state the property, not the function.
- Comments explain *why*, never *what*.
- Plain-language errors in anything a non-technical user sees; say what to do next.
- Prefer failing closed — `crypto.write_secure` refuses to write plaintext when
  encryption is enabled rather than silently degrading.
- **Growth belongs in `docs/RATIONALE.md`, not here.** `tests/test_claude_md.py`
  pins this file's length and checks every test it names exists.

## Layout

```
medrag/
  config.py providers.py documents.py jsonl.py   config, provider presets, models,
                                                 how JSONL is split and written
  ingest/    pubmed.py, pdf.py, store.py (append-only corpus, id sidecar, quarantine)
  trials/    client.py (CTgov v2: exhaustive pagination, countTotal assertion, retry),
             queries.py (query-set union, provenance, basket caveat),
             anchors.py (a query must be ABOUT something), store.py (SQLite+FTS5)
  fda/       client.py store.py bulk.py (SHARED) pma.py device_answer.py faers.py
             orangebook.py purplebook.py drugs.py drug_store.py (ApprovalAnswer)
  agents.py  THE drug/agent/device-name matcher     markers.py  THE marker vocabulary
  biomarker.py / biomarker_gating.py   patient-side and census reducers, different
                                       policies over the same signals
  phrasing.py   the self-contradicting-caveat lint
  disclosures.py evidence_grade.py chunking.py   per-document signals stamped on
                                                 every chunk at ingest
  embeddings.py vectorstore.py retriever.py router.py context.py
  ranking.py coverage.py landscape.py            scoring, coverage, the patient page
  negative_evidence.py claims.py validation.py   the negative and verification passes
  diligence.py memo.py claims_memo.py landscape_memo.py table_render.py
  crypto.py autoload.py setup_env.py dbopen.py precompute.py
config/  diligence_questions.yaml (THE QUESTION SET), trial_queries.yaml, agents.yaml,
         markers.yaml, ranking.yaml, registries.yaml, landscape.yaml,
         fda_decision_codes.yaml (VERBATIM FDA text), fda_exclusivity_codes.yaml
         (CURATED, labelled), fda_biologic_exclusivity.yaml — edit these, not code
app.py, pages/2_Verify_Claims.py, pages/3_Trial_Landscape.py
```

Per-module annotations: RATIONALE §18.

## Running it

```bash
pip install -r requirements.txt
python -m medrag doctor                     # connectivity check
python -m medrag ingest --query "..." --index
python -m medrag trials --condition "..." --intervention "..."   # --incomplete, --migrate
python -m medrag fda --product-code "FRN" --device-name "infusion pump"
python -m medrag drugs --asset "pembrolizumab"
python -m medrag pma                        # bulk download of premarket approvals
python -m medrag orangebook                 # bulk download of listed patents/exclusivity
python -m medrag purplebook                 # monthly CSV of licensed biologics/biosimilars
python -m medrag faers --asset "..."        # cache FAERS aggregate counts
python -m medrag diligence --asset "..." --indication "..."
python -m medrag verify --claims claims.txt --asset "..." --company "..."
python -m medrag landscape --condition "..." --biomarker "MSS" [--location "..."]
streamlit run app.py
```

No network? `python scripts/make_sample_corpus.py && python scripts/make_sample_trials.py
&& python -m medrag index` seeds synthetic data, with `SAMPLE-*` ids and
`[SYNTHETIC]` titles so it can never be mistaken for a real finding.

## Known-unverified, and not built

Never run for real: a live PubMed fetch; any real LLM call (prompt behaviour,
citation discipline and JSON-mode output are all unproven);
`sentence-transformers` weights; the faithfulness validator and claim verifier
against real model output (expect the numeric-grounding check to need
calibration); Streamlit double-click launch on macOS. Registry and openFDA
pagination HAVE been proven live. Fill rates and details: RATIONALE §20.

What is NOT built is stated rather than inferred — the complete FDA surface, and
the standing gap that every regulatory source here records what was approved and
none records what was refused (`transparency/crl`): RATIONALE §21.

## The one file worth changing first

`config/diligence_questions.yaml`. The current question set is a **draft** and
says so. Rewriting it with someone who does diligence for a living is the single
highest-value change available to this project.
