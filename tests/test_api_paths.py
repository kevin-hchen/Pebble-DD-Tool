"""Tests for the network-dependent paths: OpenAI and PubMed E-utilities.

These exercise the real code against mocked transports, so the request shapes,
batching, and response parsing are all verified without a key or a network
connection. The PubMed test parses a fixture captured from actual efetch XML.

Run: python tests/test_api_paths.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Direct runs do not load conftest.py, so the no-network guard is installed
# here too. See tests/netguard.py.
from tests import netguard  # noqa: E402

netguard.install()

import numpy as np  # noqa: E402

from medrag.config import Config  # noqa: E402
from medrag.documents import Chunk, Retrieved  # noqa: E402
from medrag.embeddings import OpenAIEmbedder, get_embedder  # noqa: E402
from medrag.generator import Generator  # noqa: E402
from medrag.ingest import pubmed  # noqa: E402

# A trimmed but structurally faithful efetch response: structured abstract with
# NlmCategory labels, author list, MeSH headings, and a citation-only record
# (no abstract) that the parser must skip.
EFETCH_XML = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">34449189</PMID>
      <Article>
        <Journal>
          <JournalIssue>
            <PubDate><Year>2021</Year><Month>Sep</Month></PubDate>
          </JournalIssue>
          <Title>The New England Journal of Medicine</Title>
          <ISOAbbreviation>N Engl J Med</ISOAbbreviation>
        </Journal>
        <ArticleTitle>Empagliflozin in Heart Failure with a Preserved Ejection Fraction.</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND" NlmCategory="BACKGROUND">Sodium-glucose cotransporter 2 inhibitors reduce the risk of hospitalization.</AbstractText>
          <AbstractText Label="METHODS" NlmCategory="METHODS">We randomly assigned 5988 patients to receive empagliflozin 10 mg or placebo.</AbstractText>
          <AbstractText Label="RESULTS" NlmCategory="RESULTS">A primary outcome event occurred in 415 of 2997 patients (13.8%) in the empagliflozin group (hazard ratio, 0.79; 95% CI, 0.69 to 0.90; P&lt;0.001).</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Anker</LastName><ForeName>Stefan D</ForeName></Author>
          <Author><LastName>Butler</LastName><ForeName>Javed</ForeName></Author>
        </AuthorList>
        <PublicationTypeList>
          <PublicationType>Randomized Controlled Trial</PublicationType>
        </PublicationTypeList>
      </Article>
      <MeshHeadingList>
        <MeshHeading><DescriptorName>Heart Failure</DescriptorName></MeshHeading>
        <MeshHeading><DescriptorName>Stroke Volume</DescriptorName></MeshHeading>
      </MeshHeadingList>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID Version="1">99999999</PMID>
      <Article>
        <ArticleTitle>Editorial with no abstract.</ArticleTitle>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""

CFG = Config(openai_api_key="sk-test", data_dir=Path("/tmp/medrag-test"))


# --------------------------------------------------------------- PubMed


def test_esearch_request_and_parse():
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"esearchresult": {"idlist": ["34449189", "30158147"]}}
    resp.raise_for_status.return_value = None

    with patch.object(pubmed.requests, "get", return_value=resp) as mock_get:
        pmids = pubmed.search_pubmed("empagliflozin HFpEF", retmax=25, cfg=CFG)

    assert pmids == ["34449189", "30158147"]
    url, kwargs = mock_get.call_args[0][0], mock_get.call_args[1]
    assert url.endswith("/esearch.fcgi")
    assert kwargs["params"]["db"] == "pubmed"
    assert kwargs["params"]["term"] == "empagliflozin HFpEF"
    assert kwargs["params"]["retmax"] == 25


def test_efetch_parses_structured_abstract():
    resp = MagicMock(status_code=200, content=EFETCH_XML)
    resp.raise_for_status.return_value = None

    with patch.object(pubmed.requests, "get", return_value=resp):
        docs = pubmed.fetch_pubmed(["34449189", "99999999"], cfg=CFG)

    assert len(docs) == 1, "the citation-only record must be skipped"
    doc = docs[0]
    assert doc.doc_id == "34449189"
    assert doc.journal == "N Engl J Med" and doc.year == "2021"
    assert doc.url == "https://pubmed.ncbi.nlm.nih.gov/34449189/"
    assert doc.authors == ["Stefan D Anker", "Javed Butler"]
    assert doc.citation == "Anker et al., N Engl J Med, 2021"
    assert doc.meta["mesh_terms"] == ["Heart Failure", "Stroke Volume"]
    # structured labels preserved, and the XML entity decoded
    assert "Background:" in doc.text and "Results:" in doc.text
    assert "P<0.001" in doc.text and "hazard ratio, 0.79" in doc.text


def test_efetch_batches_large_pmid_lists():
    resp = MagicMock(status_code=200, content=b"<PubmedArticleSet></PubmedArticleSet>")
    resp.raise_for_status.return_value = None

    with patch.object(pubmed.requests, "get", return_value=resp) as mock_get:
        pubmed.fetch_pubmed([str(i) for i in range(250)], cfg=CFG, batch_size=100)

    assert mock_get.call_count == 3  # 100 + 100 + 50


def test_api_key_added_to_params_when_present():
    cfg = Config(ncbi_api_key="abc123", ncbi_email="me@example.com")
    params = pubmed._params(cfg, term="x")
    assert params["api_key"] == "abc123" and params["email"] == "me@example.com"
    assert "api_key" not in pubmed._params(Config(), term="x")


# --------------------------------------------------------------- OpenAI embeddings


def _fake_embedding_response(n: int, dim: int = 1536):
    resp = MagicMock()
    resp.data = [MagicMock(embedding=list(np.random.rand(dim))) for _ in range(n)]
    return resp


def test_openai_embedder_batches_and_normalizes():
    client = MagicMock()
    client.embeddings.create.side_effect = lambda model, input: _fake_embedding_response(len(input))

    with patch("openai.OpenAI", return_value=client):
        embedder = OpenAIEmbedder(CFG)
        vecs = embedder.embed([f"chunk {i}" for i in range(200)])

    assert vecs.shape == (200, 1536)
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-5), "output must be unit-norm"
    assert client.embeddings.create.call_count == 3  # 96 + 96 + 8
    kwargs = client.embeddings.create.call_args[1]
    assert kwargs["model"] == "text-embedding-3-small"
    assert all("\n" not in t for t in kwargs["input"]), "newlines stripped before embedding"


def test_backend_auto_prefers_openai_when_key_present():
    client = MagicMock()
    with patch("openai.OpenAI", return_value=client):
        embedder = get_embedder(Config(openai_api_key="sk-test"), verbose=False)
    assert embedder.name.startswith("openai:")


def test_backend_openai_without_key_raises():
    try:
        get_embedder(Config(embed_backend="openai", openai_api_key=None), verbose=False)
    except RuntimeError as exc:
        assert "OPENAI_API_KEY" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when backend=openai and no key")


def test_auto_falls_back_when_openai_construction_fails():
    with patch("openai.OpenAI", side_effect=Exception("network down")):
        embedder = get_embedder(Config(openai_api_key="sk-test"), verbose=False)
    assert not embedder.name.startswith("openai:"), "auto must degrade, not crash"


# --------------------------------------------------------------- OpenAI generation


def _retrieved(n: int = 2) -> list[Retrieved]:
    return [
        Retrieved(
            chunk=Chunk(
                chunk_id=f"34449189::{i}",
                doc_id="34449189",
                text=f"Empagliflozin trial excerpt {i}: hazard ratio 0.79.",
                title="Empagliflozin in HFpEF",
                citation="Anker et al., N Engl J Med, 2021",
                url="https://pubmed.ncbi.nlm.nih.gov/34449189/",
            ),
            score=0.8 - 0.1 * i,
        )
        for i in range(n)
    ]


def test_generator_sends_grounded_prompt_and_parses_response():
    client = MagicMock()
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content="Yes, it reduced events [1]."))]
    completion.usage = MagicMock(prompt_tokens=812, completion_tokens=64)
    client.chat.completions.create.return_value = completion

    with patch("openai.OpenAI", return_value=client):
        gen = Generator(CFG)
        answer = gen.generate("Does empagliflozin help in HFpEF?", _retrieved())

    assert answer.text == "Yes, it reduced events [1]."
    assert answer.model == "gpt-4o-mini" and answer.cited_indices == {1}
    assert answer.usage["prompt_tokens"] == 812

    kwargs = client.chat.completions.create.call_args[1]
    assert kwargs["model"] == "gpt-4o-mini" and kwargs["temperature"] == 0.0
    system, user = kwargs["messages"]
    assert system["role"] == "system" and "ONLY the numbered context" in system["content"]
    assert "PMID 34449189" in user["content"], "citations must reach the model"
    assert "Does empagliflozin help in HFpEF?" in user["content"]


def test_generator_falls_back_to_extractive_without_key():
    gen = Generator(Config(openai_api_key=None))
    answer = gen.generate("any question", _retrieved())
    assert gen.client is None
    assert answer.model == "extractive-fallback" and answer.sources


def test_generator_reports_empty_retrieval_without_calling_model():
    client = MagicMock()
    with patch("openai.OpenAI", return_value=client):
        answer = Generator(CFG).generate("obscure question", [])
    assert answer.grounded is False
    client.chat.completions.create.assert_not_called()


def test_bibliography_renders_pmids():
    answer = Generator(Config(openai_api_key=None)).generate("q", _retrieved())
    bib = answer.bibliography()
    assert "[1]" in bib and "PMID 34449189" in bib and "pubmed.ncbi.nlm.nih.gov" in bib


if __name__ == "__main__":
    np.random.seed(0)
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as exc:
                failures += 1
                print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print("\nall API-path tests passed" if not failures else f"\n{failures} test(s) failed")
    raise SystemExit(1 if failures else 0)
