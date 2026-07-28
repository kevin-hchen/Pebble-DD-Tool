"""Persist the raw corpus as JSONL so ingestion and indexing stay decoupled.

Storage goes through medrag.crypto, so the corpus is written encrypted when a
passphrase is configured and in the clear otherwise. Either way the file is
created with mode 0600.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..crypto import read_secure, write_secure
from ..documents import Document


def save_corpus(
    docs: list[Document],
    path: str | Path,
    dedupe: bool = True,
    passphrase: str | None = None,
) -> Path:
    """Write documents to JSONL, merging with anything already stored there."""
    path = Path(path)

    existing = load_corpus(path, passphrase=passphrase) if path.exists() else []
    merged = existing + docs
    if dedupe:
        by_id: dict[str, Document] = {}
        for d in merged:
            by_id[d.doc_id] = d  # later wins, so re-ingestion refreshes records
        merged = list(by_id.values())

    payload = "\n".join(json.dumps(d.to_dict(), ensure_ascii=False) for d in merged) + "\n"
    return write_secure(path, payload.encode("utf-8"), passphrase)


def load_corpus(path: str | Path, passphrase: str | None = None) -> list[Document]:
    path = Path(path)
    if not path.exists():
        return []
    text = read_secure(path, passphrase).decode("utf-8")
    return [Document.from_dict(json.loads(l)) for l in text.splitlines() if l.strip()]
