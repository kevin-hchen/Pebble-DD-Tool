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
  chunking.py       section-aware chunking; grades evidence at ingest
  evidence_grade.py publication type -> study-design tier
  embeddings.py     OpenAI / sentence-transformers / hashing fallback chain
  vectorstore.py    FAISS IndexFlatIP + NumPy fallback, optional encryption
  retriever.py      MMR re-ranking, per-doc caps, evidence-tier boost
  router.py         STRUCTURED / SEMANTIC / BOTH
  context.py        provenance-labelled evidence assembly
  negative_evidence.py  stopped trials + contradiction hunt
  diligence.py      question-set runner, memo assembly
  memo.py           Markdown + PDF rendering
  validation.py     citation coverage, marker validity, numeric grounding
  crypto.py         AES-256-GCM at rest, fail-closed guard
  autoload.py       fetch-on-demand so users never touch a terminal
  setup_env.py      reads/writes .env from the app
config/diligence_questions.yaml   THE QUESTION SET — edit this, not code
app.py              Streamlit launcher
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
2. Live ClinicalTrials.gov API v2 fetch, pagination, and `whyStopped` fill rate.
3. Any real LLM call — OpenAI or a free provider. Prompt behaviour, citation
   discipline, and JSON-mode responses from the router and contradiction hunter
   are all unproven against a real model.
4. `sentence-transformers` local embeddings — the library installs, but the model
   weights never downloaded, so `SentenceTransformerEmbedder` has never
   constructed successfully.
5. The faithfulness validator against real model output. It has only ever seen
   synthetic text. Expect the numeric-grounding check to need calibration — a
   model writing "roughly 30%" for a source saying 28.4% will trip it.
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
