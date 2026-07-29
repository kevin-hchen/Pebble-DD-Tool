"""Tests for funding/affiliation capture at ingest and the index-schema refusal.

No network. The efetch parser runs against the captured XML fixture; the rest is
pure. The property under test throughout: a disclosure anywhere in a document is
visible on every chunk of it, so independence is judged from the whole record
rather than the one chunk that happens to be cited.
"""

from __future__ import annotations

import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.pubmed import efetch_xml  # noqa: E402

from medrag.chunking import chunk_document  # noqa: E402
from medrag.config import Config  # noqa: E402
from medrag.disclosures import (  # noqa: E402
    disclosure_from_document,
    names_public_funder,
    scan_funding_sentences,
)
from medrag.documents import Chunk, Document  # noqa: E402
from medrag.ingest import pubmed  # noqa: E402
from medrag.retriever import Retriever  # noqa: E402
from medrag.vectorstore import INDEX_SCHEMA, VectorStore  # noqa: E402


# --------------------------------------------------------------- parser capture


def test_efetch_captures_affiliations_and_coi_statement():
    """The real fixture carries author affiliations and a COI statement; both
    must land in Document.meta for the disclosure signal to use later."""
    root = ET.fromstring(efetch_xml("36571459"))
    doc = pubmed._parse_article(root.find(".//PubmedArticle"))
    assert doc.meta["affiliations"], "author affiliations must be captured"
    assert "conflict" in doc.meta["coi_statement"].lower()
    # These keys must always exist so downstream code never guesses.
    for key in ("affiliations", "grants", "coi_statement", "funding_scan"):
        assert key in doc.meta


def test_no_conflict_statement_reads_as_positive_independence():
    """This real paper declares no conflicts — positive evidence of independence,
    not a company link."""
    root = ET.fromstring(efetch_xml("36571459"))
    doc = pubmed._parse_article(root.find(".//PubmedArticle"))
    disc = disclosure_from_document(doc)
    assert disc.independent is True


# --------------------------------------------------------------- scanning & signals


def test_funding_scan_finds_the_funding_sentence():
    text = ("Results: Sensitivity was 92.3%. Conclusions: The test performed well. "
            "(Funded by Acme Corporation; ClinicalTrials.gov number NCT00000000.)")
    hits = scan_funding_sentences(text)
    assert any("Acme Corporation" in s for s in hits)


def test_public_funder_is_recognised_industry_is_not():
    assert names_public_funder("Supported by the National Cancer Institute (NCI NIH HHS).")
    assert not names_public_funder("Funded by Acme Corporation.")


def test_company_funding_is_not_flagged_independent():
    """A named industry funder is a disclosure, but NOT positive independence."""
    doc = Document(doc_id="1", title="A study", text="Results: it worked. (Funded by Acme Corp.)")
    disc = disclosure_from_document(doc)
    assert "Acme" in disc.blob
    assert disc.independent is False


def test_public_funder_document_is_independent():
    doc = Document(doc_id="1", title="A study", text="Methods and results.",
                   meta={"grants": ["NCI NIH HHS (United States)"]})
    assert disclosure_from_document(doc).independent is True


# --------------------------------------------------------------- propagation onto chunks


def test_disclosure_propagates_to_every_chunk():
    """The bug: the funder sits in Conclusions, the cited result in Results, a
    different chunk. Every chunk must carry the document-level disclosure so the
    funder travels with whichever one is cited."""
    results = "Results: " + ("The assay detected the marker across disease stages. " * 24)
    concl = " Conclusions: It performed well. (Funded by Acme Corporation; NCT00000000.)"
    doc = Document(doc_id="1", title="A study", text=results + concl)
    chunks = chunk_document(doc, Config())

    assert len(chunks) >= 2, "need a multi-chunk document to exercise the split"
    results_only = [c for c in chunks if "Acme" not in c.text]
    assert results_only, "expected at least one chunk whose own text lacks the funder"
    assert all("Acme" in c.disclosure for c in chunks), \
        "the funder must be visible on chunks that do not name it themselves"


def test_old_chunks_without_disclosure_still_load():
    """An index built before the field existed must degrade, not fail."""
    c = Chunk.from_dict({"chunk_id": "a::0", "doc_id": "a", "text": "t"})
    assert c.disclosure == "" and c.disclosure_independent is False


# --------------------------------------------------------------- index schema refusal


class _Embedder:
    name = "test-embedder"
    dim = 3

    def embed_query(self, q):  # pragma: no cover - not reached in these tests
        return np.zeros(self.dim, dtype="float32")


def test_retriever_refuses_index_predating_the_disclosure_fields():
    stale = VectorStore(dim=3, embedder_name="test-embedder", index_schema="")
    try:
        Retriever(stale, _Embedder(), Config())
    except RuntimeError as exc:
        assert "rebuild" in str(exc).lower()
    else:
        raise AssertionError("a schema-less index must be refused, not silently used")


def test_retriever_refuses_a_superseded_schema():
    old = VectorStore(dim=3, embedder_name="test-embedder", index_schema="disclosure-v0")
    try:
        Retriever(old, _Embedder(), Config())
    except RuntimeError as exc:
        assert "out of date" in str(exc).lower() and "rebuild" in str(exc).lower()
    else:
        raise AssertionError("a superseded schema must be refused")


def test_current_schema_round_trips_and_is_accepted():
    store = VectorStore(dim=2, embedder_name="test-embedder")
    store.add([Chunk(chunk_id="a::0", doc_id="a", text="t")], np.array([[1.0, 0.0]], dtype="float32"))
    d = Path(tempfile.mkdtemp())
    store.save(d)

    import json
    manifest = json.loads((d / "manifest.json").read_text())
    assert manifest["schema"] == INDEX_SCHEMA, "the manifest must record the schema"

    loaded = VectorStore.load(d)
    assert loaded.index_schema == INDEX_SCHEMA
    Retriever(loaded, _Embedder(), Config())  # must not raise


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print("\nall disclosure tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
