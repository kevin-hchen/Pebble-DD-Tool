"""Structure-aware semantic chunking.

Clinical abstracts are already structured (BACKGROUND / METHODS / RESULTS /
CONCLUSIONS). Splitting on those boundaries first, then packing sentences up to
a size budget, keeps a chunk's claim and its numbers in the same window - which
is what makes citation-level grounding work.
"""

from __future__ import annotations

import re

from .config import Config
from .documents import Chunk, Document
from .evidence_grade import grade_document

_LABELS = (
    r"background|objective[s]?|introduction|aim[s]?|purpose|method[s]?|"
    r"materials and methods|design|setting|participants|intervention[s]?|"
    r"result[s]?|finding[s]?|discussion|conclusion[s]?|interpretation|"
    r"limitations|abstract|references"
)

# Two shapes of section header:
#   line-start ("RESULTS" on its own line, colon optional) - typical of PDFs
#   mid-text after a sentence ("... risk. Results: ...") - typical of PubMed
#     abstracts flattened to a single paragraph; the colon is required there so
#     ordinary prose like "the results were mixed" is not mistaken for a header.
_SECTION_RE = re.compile(
    rf"(?:^\s*(?P<head>{_LABELS})\b\s*[:.\-]?\s*)"
    rf"|(?:(?<=[.!?])\s+(?P<inline>{_LABELS})\b\s*:\s*)",
    re.IGNORECASE | re.MULTILINE,
)


def _label_of(match: re.Match) -> str:
    return (match.group("head") or match.group("inline") or "").strip()

# Don't split on the period in "1.5 mg", "p < 0.05", "vs.", "e.g.", "Fig. 2".
_ABBREV = r"(?<!\b[A-Z])(?<!\bvs)(?<!\be\.g)(?<!\bi\.e)(?<!\bFig)(?<!\bNo)(?<!\bDr)(?<!\bet al)"
_SENT_RE = re.compile(rf"{_ABBREV}(?<=[.!?])\s+(?=[A-Z(\[])")


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    return [s.strip() for s in _SENT_RE.split(text) if s.strip()]


def split_sections(text: str) -> list[tuple[str, str]]:
    """Split text into (section_label, body) pairs; label is '' if unstructured."""
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return [("", text)]

    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        head = text[: matches[0].start()].strip()
        if head:
            sections.append(("", head))

    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        if body:
            sections.append((_label_of(m).title(), body))
    return sections


def _pack(sentences: list[str], cfg: Config) -> list[str]:
    """Greedily pack sentences to chunk_size, carrying chunk_overlap chars forward."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for sent in sentences:
        # A single oversized sentence (rare, e.g. a table dumped inline) is hard-split.
        if len(sent) > cfg.chunk_size * 1.5:
            if current:
                chunks.append(" ".join(current))
                current, size = [], 0
            for i in range(0, len(sent), cfg.chunk_size):
                chunks.append(sent[i : i + cfg.chunk_size])
            continue

        if size + len(sent) + 1 > cfg.chunk_size and current:
            chunks.append(" ".join(current))
            # carry the tail of the last chunk so a claim split across the
            # boundary is still recoverable from one side
            carry, carry_len = [], 0
            for s in reversed(current):
                if carry_len + len(s) > cfg.chunk_overlap:
                    break
                carry.insert(0, s)
                carry_len += len(s) + 1
            current, size = carry, carry_len

        current.append(sent)
        size += len(sent) + 1

    if current:
        chunks.append(" ".join(current))

    # Fold a runt tail back into its predecessor rather than indexing a fragment.
    # Bind the tail before mutating: `chunks[-2] = f"... {chunks.pop()}"` evaluates
    # the RHS first, and after pop() a 2-chunk list becomes length 1, so the LHS
    # `chunks[-2]` raises IndexError.
    if len(chunks) > 1 and len(chunks[-1]) < cfg.min_chunk_size:
        tail = chunks.pop()
        chunks[-1] = f"{chunks[-1]} {tail}"
    return chunks


def chunk_document(doc: Document, cfg: Config) -> list[Chunk]:
    chunks: list[Chunk] = []
    ordinal = 0
    # Graded once per document at ingest: publication type is already in the
    # PubMed metadata, so this costs nothing and needs no model.
    grade = grade_document(doc)

    for label, body in split_sections(doc.text):
        for piece in _pack(split_sentences(body), cfg):
            if len(piece) < cfg.min_chunk_size and chunks:
                continue
            # Prepending the title+section is cheap and measurably improves
            # retrieval: it gives short chunks topical anchoring.
            header = f"{doc.title}"
            if label:
                header += f" - {label}"
            chunks.append(
                Chunk(
                    chunk_id=f"{doc.doc_id}::{ordinal}",
                    doc_id=doc.doc_id,
                    text=f"{header}\n{piece}",
                    section=label,
                    ordinal=ordinal,
                    title=doc.title,
                    url=doc.url,
                    citation=doc.citation,
                    evidence_key=grade.key,
                    evidence_tag=grade.tag,
                    evidence_rank=grade.rank,
                )
            )
            ordinal += 1

    return chunks


def chunk_corpus(docs: list[Document], cfg: Config) -> list[Chunk]:
    out: list[Chunk] = []
    for doc in docs:
        out.extend(chunk_document(doc, cfg))
    return out
