"""PubMed ingestion via the NCBI E-utilities API.

esearch -> PMIDs, efetch -> XML records, parsed into Document objects.
No API key is required; supplying NCBI_API_KEY simply raises the rate limit
from 3 to 10 requests/second.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import Iterable

import requests

from ..config import Config, load_config
from ..documents import Document

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_LAST_CALL = {"t": 0.0}


def _throttle(cfg: Config) -> None:
    """Stay inside NCBI's rate limits (3/s anonymous, 10/s with a key)."""
    min_gap = 0.11 if cfg.ncbi_api_key else 0.34
    elapsed = time.time() - _LAST_CALL["t"]
    if elapsed < min_gap:
        time.sleep(min_gap - elapsed)
    _LAST_CALL["t"] = time.time()


def _assert_network_allowed(cfg: Config) -> None:
    """Offline mode is a hard stop, not a preference."""
    if getattr(cfg, "offline", False):
        raise RuntimeError(
            "offline mode is enabled (MEDRAG_OFFLINE=1): refusing to contact NCBI. "
            "Unset it to allow outbound requests."
        )


def _params(cfg: Config, **kwargs) -> dict:
    p = {"db": "pubmed", "tool": "medrag", **kwargs}
    if cfg.ncbi_email:
        p["email"] = cfg.ncbi_email
    if cfg.ncbi_api_key:
        p["api_key"] = cfg.ncbi_api_key
    return p


def search_pubmed(
    query: str,
    retmax: int = 50,
    cfg: Config | None = None,
    timeout: int = 30,
) -> list[str]:
    """Return PMIDs matching a PubMed query string."""
    cfg = cfg or load_config()
    _assert_network_allowed(cfg)
    _throttle(cfg)
    r = requests.get(
        f"{EUTILS}/esearch.fcgi",
        params=_params(cfg, term=query, retmax=retmax, retmode="json", sort="relevance"),
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return "".join(node.itertext()).strip()


def _unique(values) -> list[str]:
    """Non-empty values, de-duplicated, order preserved."""
    return list(dict.fromkeys(v for v in values if v))


def _parse_article(art: ET.Element) -> Document | None:
    pmid = _text(art.find(".//PMID"))
    if not pmid:
        return None

    title = _text(art.find(".//ArticleTitle"))

    # Abstracts are often structured (BACKGROUND / METHODS / RESULTS / ...).
    # Keep the labels: they make chunks far more interpretable downstream.
    parts: list[str] = []
    for ab in art.findall(".//Abstract/AbstractText"):
        label = (ab.get("Label") or ab.get("NlmCategory") or "").strip()
        body = _text(ab)
        if not body:
            continue
        parts.append(f"{label.title()}: {body}" if label else body)
    abstract = "\n\n".join(parts)
    if not abstract:
        return None  # nothing retrievable; skip citation-only records

    authors = []
    for a in art.findall(".//AuthorList/Author"):
        last, fore = _text(a.find("LastName")), _text(a.find("ForeName"))
        if last:
            authors.append(f"{fore} {last}".strip())

    journal = _text(art.find(".//Journal/ISOAbbreviation")) or _text(art.find(".//Journal/Title"))
    year = (
        _text(art.find(".//Journal/JournalIssue/PubDate/Year"))
        or _text(art.find(".//Journal/JournalIssue/PubDate/MedlineDate"))[:4]
    )
    mesh = [_text(m) for m in art.findall(".//MeshHeadingList/MeshHeading/DescriptorName")]
    pub_types = [_text(t) for t in art.findall(".//PublicationTypeList/PublicationType")]

    # Funding and conflict signals, captured here so a disclosure in any part of
    # the record travels with every chunk of the document. Independence cannot be
    # judged from the cited chunk alone — the funder and the result almost always
    # sit in different chunks — so it is decided at ingest, like evidence grading.
    from ..disclosures import scan_funding_sentences

    affiliations = _unique(_text(a) for a in art.findall(".//AuthorList/Author/AffiliationInfo/Affiliation"))
    grants = _unique(
        " ".join(p for p in (_text(g.find("Agency")), _text(g.find("GrantID")),
                             _text(g.find("Country"))) if p)
        for g in art.findall(".//GrantList/Grant")
    )
    coi = _text(art.find(".//CoiStatement"))
    funding_scan = scan_funding_sentences(abstract)

    return Document(
        doc_id=pmid,
        title=title,
        text=abstract,
        source="pubmed",
        authors=authors,
        journal=journal,
        year=year,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        meta={
            "mesh_terms": mesh,
            "publication_types": pub_types,
            "affiliations": affiliations,
            "grants": grants,
            "coi_statement": coi,
            "funding_scan": funding_scan,
        },
    )


def fetch_pubmed(
    pmids: Iterable[str],
    cfg: Config | None = None,
    batch_size: int = 100,
    timeout: int = 60,
) -> list[Document]:
    """Fetch and parse full records for a list of PMIDs."""
    cfg = cfg or load_config()
    _assert_network_allowed(cfg)
    pmids = [str(p) for p in pmids]
    docs: list[Document] = []

    for i in range(0, len(pmids), batch_size):
        batch = pmids[i : i + batch_size]
        _throttle(cfg)
        r = requests.get(
            f"{EUTILS}/efetch.fcgi",
            params=_params(cfg, id=",".join(batch), retmode="xml"),
            timeout=timeout,
        )
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for art in root.findall(".//PubmedArticle"):
            doc = _parse_article(art)
            if doc:
                docs.append(doc)

    return docs


def build_corpus(query: str, retmax: int = 50, cfg: Config | None = None) -> list[Document]:
    """Convenience: search then fetch in one call."""
    cfg = cfg or load_config()
    pmids = search_pubmed(query, retmax=retmax, cfg=cfg)
    return fetch_pubmed(pmids, cfg=cfg)
