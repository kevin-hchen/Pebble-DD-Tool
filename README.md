# MedRAG — Clinical Evidence Assistant

**Personal project.** Built on public data sources — PubMed via the NCBI
E-utilities API, and the ClinicalTrials.gov v2 registry — with no proprietary
inputs and no affiliation with any employer. Released under the MIT License.

A retrieval-augmented pipeline over biomedical evidence, built for diligence rather than chat. It answers questions from two stores — published literature and the clinical trial registry — and grounds every claim in a citable source, PMID or NCT, with an automated faithfulness check on each answer.

```
PubMed E-utilities ─┐
                    ├─▶ Document ─▶ section-aware chunking ─▶ embeddings ─▶ FAISS ─┐
local PDFs ─────────┘                                                              │
                                                                                   ▼
                       question ─▶ router ─▶ STRUCTURED / SEMANTIC / BOTH ─▶ evidence
                                                                                   │
clinicaltrials.gov v2 ─▶ TrialRecord ─▶ SQLite (phase, status, whyStopped) ────────┤
                                                                                   ▼
                        answer + [n] citations, labelled TRIAL RECORD vs LITERATURE
                                    │
                                    ▼
                          faithfulness validation
```

**Why two stores.** Trial records are facts with fields; phase and status are filters, not semantics. Embedding "Phase 3, TERMINATED" as prose and hoping cosine similarity recovers the distinction destroys exactly the precision the registry exists for. So trials live in SQLite with indexes, literature lives in FAISS, and a router decides which one answers a given question.

## Quickstart

```bash
pip install -r requirements.txt
cp .env.example .env          # add your OPENAI_API_KEY

python -m medrag doctor       # are PubMed, ClinicalTrials.gov and OpenAI reachable?

# literature
python -m medrag ingest --query "SGLT2 inhibitors heart failure" -n 50 --index

# trial registry
python -m medrag trials --condition "heart failure" --intervention empagliflozin

# the deliverable: a fixed question set, run against one asset, exported as a memo
python -m medrag diligence --asset "empagliflozin" --indication "heart failure"

python -m medrag ask "Do SGLT2 inhibitors reduce hospitalization in HFpEF?"
python -m medrag route "Who else has run trials on this mechanism?"
```

No API key or no network? Both paths still run end to end:

```bash
python scripts/make_sample_corpus.py   # 6 synthetic literature records
python scripts/make_sample_trials.py   # 5 trial records from the API v2 fixtures
python -m medrag index
python -m medrag stats
python scripts/eval.py
```

## The diligence memo

`medrag diligence` is the deliverable. It runs a fixed question set — defined in
`config/diligence_questions.yaml`, not in code — against one asset and exports
Markdown and PDF. A chat box gives a differently shaped answer every time; the
same questions in the same order against every asset is what makes two memos
comparable, and comparability is what makes output usable in an investment memo.

Four things distinguish it from a RAG demo.

**Evidence grading.** Study design is assigned at ingest from PubMed publication
types, free of charge and with no model call. Meta-analysis > systematic review >
RCT > cohort > case-control > case series > narrative. The tier is used twice: as
a gentle ranking boost, and as a visible tag in the output. The second matters
more — reranking silently changes what an analyst sees, whereas a tag lets them
disagree with the ranking.

**A negative-evidence pass.** RAG confirms whatever you ask it; for diligence the
valuable output is the evidence against the thesis. Its deterministic half is a
SQL query for trials with status TERMINATED, WITHDRAWN or SUSPENDED, with
`whyStopped` attached — no model judgement, no hallucination. Its model half hunts
for contradictions and is explicitly permitted to return nothing, because a
forced-contradiction prompt manufactures one, and an invented contradiction in a
memo is worse than silence. Results render as their own section.

**Provenance labelling.** Every excerpt is marked `TRIAL RECORD` or `LITERATURE`
with its NCT or PMID, in the prompt as well as the bibliography. A registry entry
is a fact about what was run; a review article is a reported finding. Blur them
and citations start pointing at the wrong kind of evidence.

**Honest negatives.** "Not assessed" and "nothing found" are kept distinct
throughout. Reporting an unchecked section as clean is a false negative dressed as
a pass.

## Design notes

**Structure-aware chunking.** Clinical abstracts are already segmented into Background / Methods / Results / Conclusions. Splitting on those boundaries before packing sentences to a size budget keeps a claim and its effect size inside the same retrieval window — which is what makes citation-level grounding possible at all. Sentence splitting is guarded against the abbreviations that break naive regex splitters (`1.5 mg`, `p < 0.05`, `vs.`, `Fig. 2`), and each chunk is prefixed with its document title and section so short chunks retain topical anchoring. Chunks overlap by 150 characters so a claim spanning a boundary is recoverable from one side.

**Retrieval.** Vectors are L2-normalized and indexed with `IndexFlatIP`, so inner product is exact cosine similarity. Flat is the right structure at this corpus size — exact search, no training step, no recall cliff; IVF or HNSW only earns its complexity past roughly a million vectors. Plain top-k on a PubMed corpus tends to return five chunks of the same trial, so retrieval pulls 24 candidates, re-ranks them with Maximal Marginal Relevance (λ = 0.6), and caps chunks per source document. The result is a context window that reflects more than one study.

**Grounded generation.** The system prompt is the safety surface: use only the supplied excerpts, cite every claim with an `[n]` marker, preserve effect sizes and confidence intervals verbatim, report disagreement between sources rather than averaging it, and say plainly when the retrieved literature does not answer the question. Temperature is 0.

**Faithfulness validation.** Every answer is checked automatically on three deterministic signals: citation coverage (share of claim-bearing sentences carrying a marker), citation validity (do markers point at sources actually retrieved), and numeric grounding (does every figure in the answer appear in a cited chunk, allowing for rounding). Numeric grounding targets the failure mode that matters most clinically — a hallucinated hazard ratio reads exactly like a real one.

**Graceful degradation.** Embeddings resolve OpenAI → sentence-transformers → a dependency-free hashed n-gram projection; generation falls back to returning retrieved evidence verbatim. The active backend is recorded in the index manifest, and a query embedded by a different backend than the index was built with is refused rather than silently returning nonsense. FAISS itself is optional — the store falls back to a NumPy matmul that returns identical results.

## Commands

| Command | Purpose |
| --- | --- |
| `medrag ingest --query "..." -n 50 [--index]` | Fetch PubMed records via E-utilities |
| `medrag ingest --pdf-dir path/` | Ingest local PDFs |
| `medrag trials -c "..." -i "..." [--status TERMINATED]` | Ingest clinicaltrials.gov records |
| `medrag index` | Chunk, embed, and index the stored corpus |
| `medrag diligence --asset "..." --indication "..."` | Run the question set, export a memo |
| `medrag ask "question" [-k 6]` | Answer with citations and a validation verdict |
| `medrag route "question"` | Show the routing decision without answering |
| `medrag doctor` | Check PubMed / ClinicalTrials.gov / OpenAI connectivity |
| `medrag stats` | Corpus, index, and trial store status |

Handoff notes for whoever picks this up next are in **[HANDOFF.md](HANDOFF.md)**.
Non-technical instructions are in **[HOW-TO-RUN.md](HOW-TO-RUN.md)**.

### For non-technical users

`Start MedRAG.command` (Mac) or `run.bat` (Windows) sets up a local virtual
environment on first launch and opens a form in the browser: asset, indication,
generate. Note the Mac entry point must be `.command`, not `.sh` — macOS hands a
double-clicked `.sh` to the default text editor rather than executing it. The
output is still the PDF — the launcher is a front door, not a report. It runs on
one machine with no deployment, no auth, and no server to maintain, which is the
point: a tool with no uptime cannot rot while nobody is looking after it.

Run as `python -m medrag <command>`.

## Layout

```
medrag/
  config.py        environment-driven configuration
  documents.py     Document / Chunk / Retrieved types
  ingest/          pubmed.py (E-utilities), pdf.py, store.py (JSONL corpus)
  trials/          client.py (CTgov API v2), store.py (SQLite + FTS5)
  chunking.py      section detection, sentence splitting, overlap packing
  embeddings.py    OpenAI / sentence-transformers / hashing backends
  vectorstore.py   FAISS IndexFlatIP with persistence + NumPy fallback
  retriever.py     MMR re-ranking, per-document diversity caps
  router.py        structured / semantic / both, rules + optional LLM
  context.py       provenance-labelled evidence assembly
  evidence_grade.py study-design tiers from publication type
  negative_evidence.py  stopped trials + contradiction hunt
  diligence.py     question-set runner, memo assembly
  memo.py          Markdown and PDF rendering
  generator.py     grounded prompting, citation rendering
  validation.py    citation coverage, marker validity, numeric grounding
  crypto.py        AES-256-GCM at rest, permissions, fail-closed guard
  pipeline.py      orchestration + MedRAG query facade
  cli.py           ingest / trials / index / ask / route / diligence / doctor / stats
config/            diligence_questions.yaml — the question set, edit this not code
app.py             launcher UI — a form that produces the memo, not a place to read it
Start MedRAG.command  double-clickable Mac entry point (.sh opens in an editor)
run.sh / run.bat   terminal start for Mac/Linux and Windows
load_data.sh       optional bulk loading; the app loads on demand by itself
scripts/           make_sample_corpus.py, make_sample_trials.py, eval.py
tests/             114 tests, no network or API key required
```

## Testing

```bash
python tests/test_pipeline.py     # 12 tests: chunking, embeddings, index, retrieval, validation
python tests/test_api_paths.py    # 12 tests: PubMed + OpenAI request shapes and parsing
python tests/test_crypto.py       # 19 tests: encryption, tamper detection, offline enforcement
python tests/test_trials.py       # 33 tests: CTgov parsing, SQLite store, routing, provenance
python tests/test_diligence.py    # 38 tests: grading, negative evidence, question set, memo
python -m pytest tests -q         # or run all five at once
python scripts/eval.py            # hit rate @k, MRR, faithfulness
```

Neither suite needs a network connection or an API key. The API tests drive the
real client code against mocked transports, so request shapes, embedding
batching, prompt construction, and response parsing are all covered; the PubMed
parser is tested against a fixture of genuine E-utilities XML, including a
structured abstract and a citation-only record that must be skipped. What they
cannot cover is live service behaviour — rate limits, quota errors, and model
output quality still need one real run.

## Privacy and security

The default corpus is published PubMed abstracts, so the baseline threat model is mild — but the controls are there when it isn't.

```bash
MEDRAG_ENCRYPT=1 python -m medrag index      # AES-256-GCM at rest, scrypt-derived key
python -m medrag ask "..." --offline         # hard-blocks every outbound call
```

Encryption at rest covers the corpus, chunk text, and vectors — embeddings are encrypted alongside the text because inversion attacks make them roughly as sensitive, not a de-identification step. Files are written 0600 via atomic rename, data directories 0700, and the index manifest stays readable on purpose so the tool knows whether to ask for a passphrase. Tampering is detected on read rather than silently deserialized. The API key and passphrase are kept out of `__repr__` so they can't reach a traceback.

Ingesting local PDFs prints a PHI notice and refuses to run without `--offline` or an explicit `--yes`, because a public-literature tool and a PHI-processing tool are indistinguishable from inside the code.

**[SECURITY.md](SECURITY.md) states the full threat model** — what the encryption protects against (stolen disk, stray backup, shared machine), what it explicitly does not (data in use, anything sent to OpenAI or NCBI, anonymization), and the known gaps. This project is not built or validated for PHI.

## Configuration

Set in `.env` (see `.env.example`): `OPENAI_API_KEY`, `MEDRAG_EMBED_MODEL`, `MEDRAG_CHAT_MODEL`, `MEDRAG_EMBED_BACKEND` (`auto` | `openai` | `sentence-transformers` | `hashing`), `NCBI_EMAIL` and `NCBI_API_KEY` (raises the E-utilities rate limit from 3 to 10 req/s), `MEDRAG_DATA_DIR`, plus `MEDRAG_ENCRYPT`, `MEDRAG_OFFLINE`, and `MEDRAG_PASSPHRASE`. Run `chmod 600 .env` after creating it.

## Limitations

Retrieval covers abstracts rather than full text unless PDFs are supplied, so pharmacological detail buried in a methods section is often out of reach. The validator checks that figures are grounded, not that they are used correctly — a number lifted from the wrong arm of a trial passes. There is no re-ranking model, no query decomposition for multi-hop questions, and no recency weighting.

MedRAG summarizes published literature. It is not a source of medical advice, and every citation should be verified against the original article.
