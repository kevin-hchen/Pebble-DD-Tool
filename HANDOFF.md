# MedRAG — handoff notes

One page, for whoever picks this up. Assumes no prior context.

## What it is

A diligence tool for biomedical assets. You give it an asset name and an
indication; it runs a fixed set of questions against two evidence stores —
published literature from PubMed and the ClinicalTrials.gov registry — and
produces a Markdown and PDF memo where every claim carries a PMID or an NCT
number and the passage it came from.

It is not a chatbot. The fixed question set is the point: the same questions in
the same order against every asset is what makes two memos comparable, which is
what makes the output usable in an investment memo.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env            # add OPENAI_API_KEY, then: chmod 600 .env

python -m medrag doctor         # confirms PubMed, ClinicalTrials.gov, OpenAI are reachable

python -m medrag ingest --query "<indication or mechanism>" -n 100 --index
python -m medrag trials --condition "<indication>" --intervention "<drug>"
python -m medrag diligence --asset "<drug>" --indication "<indication>"
```

The memo lands in `out/`. The whole run takes a few minutes; ingestion dominates.

To try it with no network and no key: `python scripts/make_sample_corpus.py &&
python scripts/make_sample_trials.py && python -m medrag index`, then run
`diligence` as above. The sample data is synthetic and marked as such.

## Where things live

| Path | What it does |
| --- | --- |
| `medrag/ingest/pubmed.py` | PubMed E-utilities: esearch → efetch → parse |
| `medrag/trials/client.py` | ClinicalTrials.gov API v2 client |
| `medrag/trials/store.py` | SQLite trial store, indexed, with FTS5 |
| `medrag/chunking.py` | Section-aware chunking; grades evidence at ingest |
| `medrag/evidence_grade.py` | Publication type → study-design tier |
| `medrag/router.py` | STRUCTURED / SEMANTIC / BOTH routing |
| `medrag/context.py` | Provenance-labelled evidence assembly |
| `medrag/negative_evidence.py` | The contradicting-evidence pass |
| `medrag/diligence.py` | Runs the question set, assembles the memo |
| `medrag/memo.py` | Markdown and PDF rendering |
| `config/diligence_questions.yaml` | **The question set. Edit this, not the code.** |

## The one file you will want to change

`config/diligence_questions.yaml`. Add, remove or rewrite questions there; the
memo picks up section order from the file. Each question declares its own route
and whether it gets a negative-evidence pass. Nothing about the question set is
hardcoded.

**The current question set is a draft and should be replaced.** It was written to
give the loader something real to load, not because it reflects how anyone
underwrites. Rewriting it with someone who does diligence for a living is the
single highest-value change available.

## Design decisions worth knowing before you change things

**Trials are not in the vector index, deliberately.** Phase and status are
filters, not semantics. Embedding "Phase 3, TERMINATED" as prose and hoping
cosine similarity recovers it destroys the precision the registry exists for. If
you find yourself about to embed trial records, that is why they aren't.

**The router has a rule-based fallback that runs when no model is available.**
Keep it. A router that silently degrades to always-BOTH looks like it's working
while doubling cost and diluting every answer.

**The negative-evidence pass has two halves.** The deterministic half is a SQL
query over stopped trials — it cannot hallucinate. The model half is explicitly
permitted to return nothing, because a forced-contradiction prompt invents one,
and an invented contradiction is worse than silence in the one section a partner
reads closely.

**Stopped-trial lookup ORs intervention and indication, never ANDs them.** A
trial of the same compound terminated in a different indication is among the most
valuable things a diligence pass can surface. ANDing hid it. That was a bug.

**"Not assessed" and "nothing found" are kept distinct** everywhere. Reporting an
unchecked section as clean is a false negative dressed as a pass.

## Known limitations

Retrieval covers abstracts, not full text, unless PDFs are supplied. The
faithfulness validator checks that figures are grounded, not that they are used
correctly — a number lifted from the wrong trial arm passes. There is no
multi-hop query decomposition, so a question needing several retrievals in
sequence gets one. No recency weighting: a 2011 paper competes evenly with a 2024
one. `whyStopped` is the highest-signal registry field and is frequently blank;
`medrag stats` reports the fill rate, and you should look at it before relying on
that section.

Prompt injection from ingested documents is not mitigated. That matters more the
less you trust your corpus.

## Secrets

No keys in the repo. `.env` is gitignored and holds `OPENAI_API_KEY`. Confirm
nothing sensitive entered git history before publishing:

```bash
git log --all -p | grep -iE "sk-[a-z0-9]{20}|api[_-]?key" | head
```

Optional at-rest encryption for the corpus and index is documented in
`SECURITY.md`, along with what it does and does not protect against. The trial
SQLite database is **not** covered by it — SQLite needs random access, so
envelope encryption does not apply. It is created 0600 in a 0700 directory.

## Tests

```bash
python tests/test_pipeline.py     # chunking, embeddings, index, retrieval, validation
python tests/test_api_paths.py    # PubMed + OpenAI request shapes and parsing
python tests/test_crypto.py       # encryption, tamper detection, offline enforcement
python tests/test_trials.py       # CTgov parsing, SQLite store, routing, provenance
python tests/test_diligence.py    # grading, negative evidence, question set, memo
```

All run without network access or an API key; external services are driven
through mocked transports against captured fixtures.

## Deliberately not built

Reranker fine-tuning and chunking experiments (marginal gains, large time cost).
UI work — the Streamlit app in `app.py` is a demo surface, and the memo is the
work format. Patent and freedom-to-operate search. FDA approvals integration.
Multi-user, auth, deployment.

Knowing what was cut is part of the handoff. None of these were forgotten.
