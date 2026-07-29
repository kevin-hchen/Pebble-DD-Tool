"""Normalized document and chunk types shared across the pipeline."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Document:
    """A single source document (a PubMed record or an ingested PDF)."""

    doc_id: str                  # PMID for PubMed, content hash for PDFs
    title: str
    text: str
    source: str = "pubmed"       # pubmed | pdf
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    year: str = ""
    url: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def citation(self) -> str:
        first = self.authors[0].split()[-1] if self.authors else "Unknown"
        etal = " et al." if len(self.authors) > 1 else ""
        bits = [f"{first}{etal}"]
        if self.journal:
            bits.append(self.journal)
        if self.year:
            bits.append(self.year)
        return ", ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Document":
        return cls(**d)


@dataclass
class Chunk:
    """A retrievable slice of a document."""

    chunk_id: str
    doc_id: str
    text: str
    section: str = ""
    ordinal: int = 0
    title: str = ""
    url: str = ""
    citation: str = ""
    # Evidence tier, assigned at ingest from PubMed publication types.
    evidence_key: str = "unclassified"
    evidence_tag: str = "UNGRADED"
    evidence_rank: int = 8
    # Document-level funding/affiliation/COI signal, propagated onto every chunk
    # at ingest so a disclosure in one section is visible when another is cited.
    disclosure: str = ""
    disclosure_independent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Chunk":
        # Tolerate indexes built before evidence grading existed: unknown keys
        # are dropped and missing ones fall back to the field defaults, so an
        # old index degrades to UNGRADED rather than failing to load.
        fields = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in fields})


@dataclass
class Retrieved:
    """A chunk plus its similarity score at query time."""

    chunk: Chunk
    score: float


def content_hash(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]
