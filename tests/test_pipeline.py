"""Unit tests covering chunking, embeddings, the vector store, retrieval and validation.

Run: python -m pytest tests -q   (or: python tests/test_pipeline.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

from medrag.chunking import _pack, chunk_document, split_sections, split_sentences  # noqa: E402
from medrag.config import Config  # noqa: E402
from medrag.documents import Document, Retrieved  # noqa: E402
from medrag.embeddings import HashingEmbedder, l2_normalize  # noqa: E402
from medrag.generator import Answer, build_context  # noqa: E402
from medrag.retriever import Retriever, mmr_select  # noqa: E402
from medrag.validation import validate_answer  # noqa: E402
from medrag.vectorstore import VectorStore  # noqa: E402

CFG = Config()

DOCS = [
    Document(
        doc_id="30158147",
        title="Empagliflozin and hospitalization for heart failure",
        text=(
            "Background: Patients with type 2 diabetes are at elevated risk of heart failure. "
            "Methods: We randomly assigned 4687 patients to empagliflozin 10 mg or placebo and "
            "followed them for a median of 3.1 years. "
            "Results: Hospitalization for heart failure occurred in 5.7% of the empagliflozin "
            "group versus 8.5% of the placebo group (hazard ratio 0.65, 95% CI 0.50 to 0.85, "
            "p = 0.002). "
            "Conclusions: Empagliflozin reduced hospitalization for heart failure."
        ),
        authors=["Bernard Zinman", "Christoph Wanner"],
        journal="N Engl J Med",
        year="2015",
        url="https://pubmed.ncbi.nlm.nih.gov/30158147/",
    ),
    Document(
        doc_id="34449189",
        title="Metformin as first-line therapy in type 2 diabetes",
        text=(
            "Background: Metformin remains the recommended initial pharmacotherapy. "
            "Methods: A cohort of 12000 adults starting metformin was followed for 5 years. "
            "Results: Mean HbA1c fell by 1.1 percentage points at 12 months. "
            "Conclusions: Metformin produces durable glycemic control at low cost."
        ),
        authors=["Amy Lee"],
        journal="Diabetes Care",
        year="2021",
        url="https://pubmed.ncbi.nlm.nih.gov/34449189/",
    ),
]


def test_sentence_splitting_preserves_numbers():
    sents = split_sentences("The dose was 1.5 mg daily. Response was seen (p < 0.05) at week 4.")
    assert len(sents) == 2, sents
    assert "1.5 mg" in sents[0]


def test_section_splitting():
    labels = [lbl for lbl, _ in split_sections(DOCS[0].text)]
    assert "Methods" in labels and "Results" in labels and "Conclusions" in labels


def test_runt_tail_merges_with_two_chunks():
    # Regression: `chunks[-2] = f"{chunks[-2]} {chunks.pop()}"` evaluated the RHS
    # before the LHS store, so a 2-chunk list dropped to length 1 and IndexError'd.
    cfg = Config(chunk_size=80, chunk_overlap=10, min_chunk_size=40)
    sentences = (
        [f"Sentence number {i} explaining the study design in detail." for i in range(3)]
        + ["Short tail."]
    )
    chunks = _pack(sentences, cfg)
    assert len(chunks) >= 1
    assert all(len(c) >= cfg.min_chunk_size for c in chunks[:-1])


def test_runt_tail_survives_real_pubmed_efetch_xml():
    # Regression: the same bug reproduced by running the full pipeline against a
    # captured live PubMed response. PMID 36571459's Results section, at default
    # chunk_size=900, packs into two chunks with a ~78-char tail, which triggers
    # the runt-tail merge. Fixture is the raw efetch XML - do not edit it.
    import xml.etree.ElementTree as ET

    from medrag.ingest.pubmed import _parse_article
    from tests.fixtures.pubmed import efetch_xml

    root = ET.fromstring(efetch_xml("36571459"))
    articles = root.findall(".//PubmedArticle")
    assert articles, "fixture must contain at least one PubmedArticle"
    doc = _parse_article(articles[0])
    assert doc is not None and doc.doc_id == "36571459"
    # Must not crash and must produce at least one chunk.
    chunks = chunk_document(doc, Config())
    assert chunks and all(c.doc_id == "36571459" for c in chunks)


def test_chunking_produces_titled_chunks():
    chunks = chunk_document(DOCS[0], CFG)
    assert chunks
    assert all(c.doc_id == "30158147" for c in chunks)
    assert all(c.text.startswith(DOCS[0].title) for c in chunks)
    assert all(len(c.text) <= CFG.chunk_size * 2 for c in chunks)


def test_embeddings_are_normalized_and_deterministic():
    emb = HashingEmbedder(dim=256)
    v1 = emb.embed(["heart failure hospitalization"])
    v2 = emb.embed(["heart failure hospitalization"])
    assert v1.shape == (1, 256)
    assert np.allclose(np.linalg.norm(v1, axis=1), 1.0, atol=1e-5)
    assert np.allclose(v1, v2)


def test_normalize_handles_zero_vector():
    out = l2_normalize(np.zeros((1, 8)))
    assert np.isfinite(out).all()


def test_vectorstore_roundtrip_and_search(tmp_path=None):
    import tempfile

    tmp = Path(tmp_path or tempfile.mkdtemp())
    emb = HashingEmbedder(dim=512)
    chunks = [c for d in DOCS for c in chunk_document(d, CFG)]
    store = VectorStore(dim=emb.dim, embedder_name=emb.name)
    store.add(chunks, emb.embed([c.text for c in chunks]))
    store.save(tmp)

    reloaded = VectorStore.load(tmp)
    assert len(reloaded) == len(chunks)

    hits = reloaded.search(emb.embed_query("empagliflozin heart failure hospitalization"), k=3)
    assert hits and hits[0].chunk.doc_id == "30158147"
    assert hits[0].score >= hits[-1].score


def test_retriever_diversity_caps_per_document():
    emb = HashingEmbedder(dim=512)
    chunks = [c for d in DOCS for c in chunk_document(d, CFG)]
    store = VectorStore(dim=emb.dim, embedder_name=emb.name)
    store.add(chunks, emb.embed([c.text for c in chunks]))

    cfg = Config(top_k=4, score_floor=-1.0)
    hits = Retriever(store, emb, cfg).retrieve("diabetes outcomes", max_per_doc=1)
    assert len(set(h.chunk.doc_id for h in hits)) == len(hits)


def test_mmr_trades_relevance_for_diversity():
    q = l2_normalize(np.array([[1.0, 0.1, 0.0]]))[0]
    # index 0 and 1 are near-duplicates of each other; index 2 is the outlier
    cands = l2_normalize(np.array([[1.0, 0.0, 0.0], [0.99, 0.14, 0.0], [0.3, 0.95, 0.0]]))

    diverse = mmr_select(q, cands, k=2, lambda_mult=0.2)
    assert 2 in diverse, "diversity-weighted MMR should surface the outlier"

    relevance_only = mmr_select(q, cands, k=2, lambda_mult=1.0)
    assert set(relevance_only) == {0, 1}, "lambda=1 should behave like plain top-k"


def test_context_respects_char_budget():
    chunks = [c for d in DOCS for c in chunk_document(d, CFG)]
    retrieved = [Retrieved(chunk=c, score=0.5) for c in chunks]
    ctx = build_context(retrieved, max_chars=300)
    assert len(ctx) <= 400 and ctx.startswith("[1]")


def test_validation_flags_hallucinated_number():
    chunks = chunk_document(DOCS[0], CFG)
    retrieved = [Retrieved(chunk=c, score=0.8) for c in chunks]

    good = Answer(
        text="Empagliflozin reduced heart failure hospitalization (hazard ratio 0.65) [1].",
        sources=retrieved,
    )
    assert validate_answer(good).passed

    bad = Answer(
        text="Empagliflozin reduced heart failure hospitalization by 47.3% [1].",
        sources=retrieved,
    )
    assert "47.3" in validate_answer(bad).ungrounded_numbers


def test_validation_flags_uncited_claims_and_bad_markers():
    chunks = chunk_document(DOCS[0], CFG)
    retrieved = [Retrieved(chunk=c, score=0.8) for c in chunks]

    uncited = Answer(
        text="Empagliflozin substantially reduces hospitalization across all patient groups studied.",
        sources=retrieved,
    )
    assert not uncited.cited_indices
    assert validate_answer(uncited).citation_coverage == 0.0

    bogus = Answer(text="A claim grounded nowhere in the retrieved set [99].", sources=retrieved)
    assert 99 in validate_answer(bogus).invalid_citations


def test_empty_retrieval_is_handled():
    assert validate_answer(Answer(text="No relevant passages.", sources=[])).n_claims == 0


def test_citation_ranges_and_comma_lists_expand_to_indices():
    # Regression: real models write [1-6], [1–6] (en-dash), [12, 14, 17]. The old
    # regex `\[(\d+)\]` matched none of these, so half the memo's cited claims
    # looked uncited and the invalid-marker check missed out-of-range integers
    # hiding inside ranges.
    from medrag.generator import parse_citation_indices

    assert parse_citation_indices("Result [1-6].") == {1, 2, 3, 4, 5, 6}
    assert parse_citation_indices("Result [1–6].") == {1, 2, 3, 4, 5, 6}  # en-dash
    assert parse_citation_indices("Result [12, 14, 17].") == {12, 14, 17}
    assert parse_citation_indices("Result [1-3, 7].") == {1, 2, 3, 7}
    assert parse_citation_indices("Result [3].") == {3}
    assert parse_citation_indices("Result [missing].") == set()
    # Malformed span must not allocate 9999 integers; skipped outright.
    assert parse_citation_indices("Result [1-9999].") == set()
    # Reversed range is skipped, not silently reordered.
    assert parse_citation_indices("Result [6-1].") == set()


def test_range_citations_do_not_leak_into_ungrounded_numbers():
    # Second-order bug the same regex caused: _numbers stripped only [n], so
    # [11-20] left "11" and "20" behind as if they were figures in the answer.
    chunks = chunk_document(DOCS[0], CFG)
    retrieved = [Retrieved(chunk=c, score=0.8) for c in chunks]
    ans = Answer(
        text="The trial evidence base is broad [11-20].",
        sources=retrieved,
    )
    report = validate_answer(ans, retrieved=retrieved)
    assert "11" not in report.ungrounded_numbers
    assert "20" not in report.ungrounded_numbers


def test_hedge_matches_absence_of_evidence_phrasings():
    # Regression: sentences that honestly state "the excerpts do not contain
    # this" were counted as uncited claims and dragged the pass rate down.
    from medrag.validation import _HEDGE_RE

    for opener in (
        "The provided excerpts do not describe the mechanism of action.",
        "The given excerpts primarily focus on outcomes.",
        "What is missing is direct information on regulator-accepted endpoints.",
        "There is no evidence in the provided excerpts to suggest observational designs.",
    ):
        assert _HEDGE_RE.match(opener), f"should hedge: {opener!r}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {exc}")
    print("\nall tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
