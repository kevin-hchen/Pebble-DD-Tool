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
side (they HAVE the biomarker): ELIGIBLE when a positive variant is named
(MSS / microsatellite stable / pMMR / proficient mismatch repair / non-MSI-H),
EXCLUDED when the trial requires the opposite, and UNCLEAR when the biomarker area
is referenced but eligibility can't be read off it — most importantly when MSS is
expressed *indirectly* by excluding MSI-H. UNCLEAR trials stay in the landscape,
flagged, because a missed trial is worse than an uncertain one for a patient
looking for an option. The matched criterion sentence is returned with every
result: a filtered list with no shown evidence cannot be checked. EXCLUDED and
not-mentioned trials are *counted*, never hidden without a number. `non-MSI-H`
contains `MSI-H`, so positive spans are stripped before the opposite is tested.

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
  ingest/           pubmed.py (E-utilities), pdf.py, store.py (JSONL corpus)
  trials/           client.py (CTgov API v2), store.py (SQLite + FTS5)
  chunking.py       section-aware chunking; grades evidence + stamps disclosure at ingest
  evidence_grade.py publication type -> study-design tier
  disclosures.py    funder/affiliation/COI signal per document, for the independence axis
  biomarker.py      eligibility-text biomarker matching (MSS variants), ELIGIBLE/UNCLEAR/…
  landscape.py      patient trial landscape — condition + biomarker -> enterable trials
  landscape_memo.py landscape table -> Markdown + PDF (reuses table_render.py)
  table_render.py   shared Markdown + reportlab table renderer (claims + landscape)
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
app.py              Streamlit launcher (memo page)
pages/2_Verify_Claims.py          Streamlit claim-verification page
pages/3_Trial_Landscape.py        Streamlit patient trial-landscape page
```

## Conventions

- **Tests never touch the network.** External services are driven through mocked
  transports against captured fixtures (`tests/fixtures/ctgov.py`). Every suite
  runs with no API key and no internet. Keep it that way — add a fixture rather
  than a live call.
- Each test file is runnable directly (`python tests/test_trials.py`) as well as
  under pytest.
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
   trial) is now parsed and preferred over the study chair; still unproven is
   pagination past one page and the fill rate on non-oncology conditions.
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
FDA approvals integration, multi-user auth and deployment. These were scoped out
to protect a three-week window, not forgotten. `openFDA` integration is the most
valuable of them if scope reopens; it should be a third structured store shaped
like `trials/`, not bolted onto the vector path.

## The one file worth changing first

`config/diligence_questions.yaml`. The current question set is a **draft** and
says so. Rewriting it with someone who does diligence for a living is the single
highest-value change available to this project.
