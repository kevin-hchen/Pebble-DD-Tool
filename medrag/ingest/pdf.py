"""PDF ingestion: extract text from local clinical papers."""

from __future__ import annotations

import re
from pathlib import Path

from ..documents import Document, content_hash

_LIGATURES = {"ﬁ": "fi", "ﬂ": "fl", "’": "'", "“": '"', "”": '"'}


def clean_pdf_text(text: str) -> str:
    """Repair the usual PDF extraction damage: hyphen breaks, hard wraps, ligatures."""
    for bad, good in _LIGATURES.items():
        text = text.replace(bad, good)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)          # de-hyphenate across lines
    text = re.sub(r"(?<![.!?:\n])\n(?![\n\s•\-\d])", " ", text)  # unwrap soft breaks
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def load_pdf(path: str | Path) -> Document:
    """Load a single PDF into a Document."""
    from pypdf import PdfReader

    path = Path(path)
    reader = PdfReader(str(path))
    pages = [(p.extract_text() or "") for p in reader.pages]
    text = clean_pdf_text("\n\n".join(pages))

    info = reader.metadata or {}
    title = (getattr(info, "title", None) or "").strip() or path.stem.replace("_", " ")
    author = (getattr(info, "author", None) or "").strip()

    return Document(
        doc_id=f"pdf-{content_hash(str(path.resolve()) + text[:2000])}",
        title=title,
        text=text,
        source="pdf",
        authors=[author] if author else [],
        url=path.as_uri(),
        meta={"filename": path.name, "n_pages": len(pages)},
    )


def load_pdf_dir(directory: str | Path) -> list[Document]:
    """Load every PDF in a directory (non-recursive), skipping unreadable files."""
    directory = Path(directory)
    docs: list[Document] = []
    for pdf in sorted(directory.glob("*.pdf")):
        try:
            doc = load_pdf(pdf)
        except Exception as exc:  # a corrupt PDF should not kill the batch
            print(f"[warn] failed to read {pdf.name}: {exc}")
            continue
        if doc.text.strip():
            docs.append(doc)
        else:
            print(f"[warn] no extractable text in {pdf.name} (scanned? needs OCR)")
    return docs
