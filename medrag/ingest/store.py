"""Persist the raw corpus as JSONL so ingestion and indexing stay decoupled.

Storage goes through medrag.crypto, so the corpus is written encrypted when a
passphrase is configured and in the clear otherwise. Either way the file is
created with mode 0600.

Three decisions here exist because of a real corruption incident. Read them
before changing this module.

**An append appends.** `save_corpus` used to read the entire corpus back,
merge in memory, and rewrite the whole file on every ingest. That made a single
unreadable line fatal to every future ingest — the read-back raised before the
new records were ever written — and it grew the corruption window in proportion
to the corpus. New records are now appended, and the set of known doc_ids lives
in a small sidecar (`corpus.jsonl.ids`) so dedup never has to rehydrate every
Document. An encrypted corpus is the one exception: AES-GCM wraps the whole
file, so there is nothing to append into and the rewrite path is still used.

**One record per doc_id, latest wins — enforced at read.** That invariant used
to be maintained by rewriting the file. It is now applied when reading, which
gives the same result to every caller and lets the writer stay append-only. A
re-ingested document still refreshes the old one.

**A malformed record is skipped, counted, and kept.** One truncated line used to
deny access to every other document in the file. The loader now sets bad lines
aside in a quarantine file beside the corpus and reports how many. It is never
silent: this project's whole discipline is that missing evidence stays visible,
and a loader that quietly drops three documents is the same class of failure as
a memo that quietly drops thirty trials. `CorpusHealth.message()` is the plain
language that reaches `medrag stats`, the Streamlit settings panel, and the
warnings block of any memo built while records are quarantined.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..crypto import CryptoError, is_encrypted, read_secure, write_secure
from ..documents import Document
from ..jsonl import dumps_line, iter_lines, split_lines

QUARANTINE_SUFFIX = ".quarantine.jsonl"
IDS_SUFFIX = ".ids"
PENDING_SUFFIX = ".pending"


@dataclass
class CorpusHealth:
    """How much of the corpus was readable, and what was set aside.

    `loaded` and `quarantined` are counted on the read that produced them, never
    cached, so a corpus repaired by a re-ingest stops reporting stale damage.
    """

    loaded: int = 0
    quarantined: int = 0
    quarantine_path: Path | None = None
    locked: bool = False        # encrypted, and no passphrase was supplied

    @property
    def clean(self) -> bool:
        return self.quarantined == 0

    def message(self) -> str | None:
        """Plain language for someone who will not open a terminal, or None.

        Returns None when there is nothing to report, so callers can treat a
        falsy value as "say nothing" rather than having to compare counts.
        """
        if self.locked or self.clean:
            return None
        n = self.quarantined
        rec = "record" if n == 1 else "records"
        was = "was" if n == 1 else "were"
        kept = self.quarantine_path.name if self.quarantine_path else "a quarantine file"
        return (
            f"{n} stored {rec} could not be read and {was} set aside; {self.loaded} "
            f"loaded normally. This usually means an earlier download was interrupted "
            f"while writing. Downloading the research again restores the missing "
            f"{rec}. The unreadable {rec} {was} kept in {kept} in case you want them."
        )


# --------------------------------------------------------------- read


def read_corpus(
    path: str | Path, passphrase: str | None = None
) -> tuple[list[Document], CorpusHealth]:
    """Load the corpus, quarantining anything unreadable.

    A line can fail two different ways and both are tolerated: it can be invalid
    JSON (a truncated write), or it can be valid JSON that is not a Document
    (a schema change, or a bare value). Neither is allowed to deny access to the
    rest of the file.
    """
    path = Path(path)
    if not path.exists():
        return [], CorpusHealth()

    text = read_secure(path, passphrase).decode("utf-8", errors="replace")

    docs: list[Document] = []
    bad: list[tuple[int, str, str]] = []
    for n, line in iter_lines(text):
        try:
            docs.append(Document.from_dict(json.loads(line)))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            bad.append((n, line, f"{type(exc).__name__}: {exc}"))

    # One record per doc_id, latest wins, so a re-ingest refreshes rather than
    # duplicates. Applied here because the writer is append-only.
    by_id: dict[str, Document] = {}
    for d in docs:
        by_id[d.doc_id] = d

    health = CorpusHealth(loaded=len(by_id), quarantined=len(bad))
    if bad:
        health.quarantine_path = _write_quarantine(path, bad, passphrase)
    return list(by_id.values()), health


def load_corpus(path: str | Path, passphrase: str | None = None) -> list[Document]:
    """The documents alone. Use `read_corpus` when the caller can report health."""
    return read_corpus(path, passphrase)[0]


def corpus_health(path: str | Path, passphrase: str | None = None) -> CorpusHealth:
    """Health without keeping the documents, for status displays.

    An encrypted corpus with no passphrase is reported as locked rather than as
    clean — the same not-assessed-versus-nothing-found rule the memo applies.
    """
    path = Path(path)
    if not path.exists():
        return CorpusHealth()
    try:
        return read_corpus(path, passphrase)[1]
    except CryptoError:
        return CorpusHealth(locked=True)


def _write_quarantine(
    path: Path, bad: list[tuple[int, str, str]], passphrase: str | None
) -> Path:
    """Set unreadable lines aside beside the corpus, verbatim.

    Encrypted with the corpus's own passphrase when there is one: these lines are
    corpus text, and writing them out in the clear would quietly undo the at-rest
    protection the operator asked for.
    """
    out = path.with_name(path.name + QUARANTINE_SUFFIX)
    payload = "\n".join(
        dumps_line({"line": n, "error": err, "raw": raw})
        for n, raw, err in bad
    ) + "\n"
    return write_secure(
        out, payload.encode("utf-8"), passphrase, allow_plaintext=passphrase is None
    )


# --------------------------------------------------------------- write


def save_corpus(
    docs: list[Document],
    path: str | Path,
    dedupe: bool = True,
    passphrase: str | None = None,
) -> Path:
    """Add documents to the corpus.

    `dedupe` is retained for callers but no longer changes the file: one record
    per doc_id with the latest winning is applied on read, which holds whether
    the writer appended or rewrote.
    """
    path = Path(path)
    docs = list(docs)

    # Records stranded by an earlier failed write get a second chance here rather
    # than being abandoned on disk.
    docs = _absorb_pending(path, passphrase) + docs

    encrypted = passphrase is not None or (path.exists() and is_encrypted(path))
    if encrypted:
        return _rewrite(path, docs, passphrase)
    return _append(path, docs)


def _rewrite(path: Path, docs: list[Document], passphrase: str | None) -> Path:
    """Read-merge-write, for an encrypted corpus only.

    AES-GCM covers the whole file, so there is no way to append a record without
    re-sealing everything. The read is tolerant, so one bad line no longer blocks
    the write the way it used to.
    """
    existing = read_corpus(path, passphrase)[0] if path.exists() else []
    by_id: dict[str, Document] = {}
    for d in existing + docs:
        by_id[d.doc_id] = d

    payload = "\n".join(
        dumps_line(d.to_dict()) for d in by_id.values()
    ) + "\n"
    out = write_secure(path, payload.encode("utf-8"), passphrase)
    _write_ids(path, set(by_id), passphrase)
    return out


def _append(path: Path, docs: list[Document]) -> Path:
    if not docs:
        return path

    known = _read_ids(path)
    payload = "\n".join(
        dumps_line(d.to_dict()) for d in docs
    ) + "\n"

    _terminate_partial_line(path)
    _append_bytes(path, payload.encode("utf-8"))
    _write_ids(path, known | {d.doc_id for d in docs}, None)
    return path


def _append_bytes(path: Path, payload: bytes) -> None:
    """Append, rolling the file back to its previous length if the write fails.

    An append cannot be made atomic the way a rename can, so the length is
    recorded first and restored on failure. A hard kill skips the rollback and
    can still leave a partial final line — which is exactly what the tolerant
    loader is for, and why these two fixes belong together.
    """
    before = path.stat().st_size if path.exists() else 0
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with open(path, "r+b") as fh:
            fh.truncate(before)
            fh.flush()
            os.fsync(fh.fileno())
        raise


def _terminate_partial_line(path: Path) -> None:
    """Close an unterminated final line before appending after it.

    Without this, the first new record is glued onto the tail of a truncated one:
    the damaged record stays damaged and takes a good new record down with it,
    turning one lost document into two.
    """
    if not path.exists() or path.stat().st_size == 0:
        return
    with open(path, "rb") as fh:
        fh.seek(-1, os.SEEK_END)
        terminated = fh.read(1) == b"\n"
    if not terminated:
        _append_bytes(path, b"\n")


# --------------------------------------------------------------- id sidecar


def _ids_path(path: Path) -> Path:
    return path.with_name(path.name + IDS_SUFFIX)


def _read_ids(path: Path) -> set[str]:
    """The doc_ids already stored, without rehydrating a single Document."""
    sidecar = _ids_path(path)
    if sidecar.exists():
        try:
            text = read_secure(sidecar, None).decode("utf-8", errors="replace")
            return {line.strip() for line in split_lines(text)}
        except (CryptoError, OSError):
            pass  # fall through and rebuild it
    return _scan_ids(path)


def _scan_ids(path: Path) -> set[str]:
    """Rebuild the sidecar's contents by reading ids only.

    Runs once, for a corpus written before the sidecar existed. It still touches
    every line, but it constructs no Documents and its result is cached to disk.
    """
    if not path.exists() or is_encrypted(path):
        return set()
    ids: set[str] = set()
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in split_lines(text):
        try:
            got = json.loads(line).get("doc_id")
        except (json.JSONDecodeError, AttributeError):
            continue
        if got:
            ids.add(str(got))
    return ids


def _write_ids(path: Path, ids: set[str], passphrase: str | None) -> None:
    payload = "\n".join(sorted(ids)) + "\n" if ids else ""
    write_secure(
        _ids_path(path),
        payload.encode("utf-8"),
        passphrase,
        allow_plaintext=passphrase is None,
    )


# --------------------------------------------------------------- pending stash


def _pending_path(path: Path) -> Path:
    return path.with_name(path.name + PENDING_SUFFIX)


def stash_pending(
    docs: list[Document], path: str | Path, passphrase: str | None = None
) -> Path:
    """Park fetched records beside the corpus when the corpus write failed.

    Fetching abstracts costs a network round trip and someone's patience; a local
    write failure should not throw them away. The next `save_corpus` picks these
    up automatically.
    """
    path = Path(path)
    existing = _read_pending(path, passphrase)
    by_id: dict[str, Document] = {}
    for d in existing + list(docs):
        by_id[d.doc_id] = d
    payload = "\n".join(
        dumps_line(d.to_dict()) for d in by_id.values()
    ) + "\n"
    return write_secure(
        _pending_path(path),
        payload.encode("utf-8"),
        passphrase,
        allow_plaintext=passphrase is None,
    )


def _read_pending(path: Path, passphrase: str | None) -> list[Document]:
    pending = _pending_path(path)
    if not pending.exists():
        return []
    try:
        text = read_secure(pending, passphrase).decode("utf-8", errors="replace")
    except CryptoError:
        return []
    docs = []
    for line in split_lines(text):
        try:
            docs.append(Document.from_dict(json.loads(line)))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return docs


def _absorb_pending(path: Path, passphrase: str | None) -> list[Document]:
    """Take back anything stashed by a previous failure, then drop the stash."""
    docs = _read_pending(path, passphrase)
    if docs:
        _pending_path(path).unlink(missing_ok=True)
    return docs


def compact_corpus(path: str | Path, passphrase: str | None = None) -> Path:
    """Rewrite the corpus as one record per doc_id.

    Append-only writing means a repeatedly re-ingested document occupies several
    physical lines. Readers never see them, so this is housekeeping, not a
    correctness fix — and it drops quarantined lines for good, which is why it is
    never run automatically.
    """
    path = Path(path)
    docs = read_corpus(path, passphrase)[0]
    payload = "\n".join(
        dumps_line(d.to_dict()) for d in docs
    ) + "\n"
    out = write_secure(
        path, payload.encode("utf-8"), passphrase, allow_plaintext=passphrase is None
    )
    _write_ids(path, {d.doc_id for d in docs}, passphrase)
    return out
