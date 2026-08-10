"""End-to-end orchestration: ingest -> chunk -> embed -> index -> retrieve -> answer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .chunking import chunk_corpus
from .config import Config, load_config
from .crypto import get_passphrase
from .documents import Document, Retrieved
from .embeddings import get_embedder
from .generator import Answer, Generator
from .ingest.pdf import load_pdf_dir
from .ingest.pubmed import fetch_pubmed, search_pubmed
from .ingest.store import load_corpus, save_corpus, stash_pending
from .retriever import Retriever

# Re-exported from a leaf module so the public service can learn a filename
# without importing this one — see medrag/storenames.py. One definition, two
# import paths, no drift.
from .storenames import CORPUS_FILE, DRUGS_DB, FDA_DB, TRIALS_DB  # noqa: F401
from .validation import ValidationReport, validate_answer
from .vectorstore import VectorStore

PHI_WARNING = """
┌─ PRIVACY NOTICE ─────────────────────────────────────────────────────────────┐
│ You are ingesting local PDFs. Unlike PubMed abstracts, local documents may   │
│ contain protected health information.                                        │
│                                                                              │
│ Unless offline mode is enabled, this text WILL be transmitted to OpenAI for  │
│ embedding and again inside answer prompts. Do not ingest patient records,    │
│ case notes, or other identifiable clinical documents unless your agreement   │
│ with that provider permits it.                                               │
│                                                                              │
│   MEDRAG_OFFLINE=1  keeps everything on this machine                         │
│   MEDRAG_ENCRYPT=1  encrypts the stored corpus and index at rest             │
└──────────────────────────────────────────────────────────────────────────────┘
""".strip()


@dataclass
class Result:
    question: str
    answer: Answer
    retrieved: list[Retrieved]
    validation: ValidationReport


def _passphrase_for(cfg: Config, confirm: bool = False) -> str | None:
    """Resolve the at-rest passphrase, prompting only when encryption is on."""
    if not cfg.encrypt:
        return None
    if cfg.passphrase:
        return cfg.passphrase
    cfg.passphrase = get_passphrase(required=True, confirm=confirm)
    return cfg.passphrase


def ingest_pubmed(query: str, retmax: int = 50, cfg: Config | None = None) -> list[Document]:
    cfg = cfg or load_config()
    cfg.ensure_dirs()
    pmids = search_pubmed(query, retmax=retmax, cfg=cfg)
    print(f"[medrag] {len(pmids)} PMIDs matched")
    docs = fetch_pubmed(pmids, cfg=cfg)
    print(f"[medrag] {len(docs)} records with abstracts fetched")

    corpus = cfg.raw_dir / CORPUS_FILE
    passphrase = _passphrase_for(cfg, confirm=True)
    try:
        save_corpus(docs, corpus, passphrase=passphrase)
    except Exception as exc:
        # The fetch already happened. A local write failure must not throw the
        # abstracts away — park them beside the corpus so the next ingest picks
        # them up instead of asking PubMed for the same records again.
        try:
            stash = stash_pending(docs, corpus, passphrase=passphrase)
        except Exception:
            raise exc
        raise RuntimeError(
            f"{len(docs)} abstracts were downloaded but could not be saved to "
            f"{corpus.name} ({exc}). They have been kept in {stash.name} and will be "
            "added automatically the next time research is downloaded, so nothing "
            "needs to be fetched again."
        ) from exc
    return docs


def ingest_pdfs(
    directory: str | Path, cfg: Config | None = None, acknowledge_phi: bool = False
) -> list[Document]:
    cfg = cfg or load_config()
    cfg.ensure_dirs()

    if not acknowledge_phi:
        print(PHI_WARNING)
        if not cfg.offline:
            print(
                "\n[medrag] offline mode is OFF - document text will be sent to OpenAI.\n"
                "         Re-run with --yes to proceed, or set MEDRAG_OFFLINE=1 first."
            )
            raise RuntimeError("PDF ingestion halted pending acknowledgement (pass --yes)")

    docs = load_pdf_dir(directory)
    print(f"[medrag] {len(docs)} PDFs ingested from {directory}")
    save_corpus(docs, cfg.raw_dir / CORPUS_FILE, passphrase=_passphrase_for(cfg, confirm=True))
    return docs


def build_index(cfg: Config | None = None, docs: list[Document] | None = None) -> VectorStore:
    cfg = cfg or load_config()
    cfg.ensure_dirs()
    passphrase = _passphrase_for(cfg, confirm=True)

    docs = docs if docs is not None else load_corpus(cfg.raw_dir / CORPUS_FILE, passphrase)
    if not docs:
        raise RuntimeError(
            f"corpus is empty — run `medrag ingest --query ...` first "
            f"(looked in {cfg.raw_dir / CORPUS_FILE})"
        )

    chunks = chunk_corpus(docs, cfg)
    print(f"[medrag] {len(docs)} documents -> {len(chunks)} chunks")

    embedder = get_embedder(cfg)
    vectors = embedder.embed([c.text for c in chunks])

    store = VectorStore(dim=embedder.dim, embedder_name=embedder.name)
    store.add(chunks, vectors)
    store.save(cfg.index_dir, passphrase=passphrase)
    print(
        f"[medrag] index saved to {cfg.index_dir} ({len(store)} vectors, dim {store.dim}"
        f"{', encrypted' if passphrase else ''})"
    )
    return store


class MedRAG:
    """Query-time facade: loads the index once, answers many questions."""

    def __init__(self, cfg: Config | None = None):
        self.cfg = cfg or load_config()
        self.cfg.ensure_dirs()

        manifest_path = self.cfg.index_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"no index at {self.cfg.index_dir} — run `medrag index` first")

        # The manifest records whether the payload is encrypted, so we only
        # prompt for a passphrase when one is actually needed.
        import json

        if json.loads(manifest_path.read_text()).get("encrypted"):
            self.cfg.encrypt = True
        passphrase = _passphrase_for(self.cfg)

        self.store = VectorStore.load(self.cfg.index_dir, passphrase=passphrase)
        self.embedder = get_embedder(self.cfg, verbose=False)
        self.retriever = Retriever(self.store, self.embedder, self.cfg)
        self.generator = Generator(self.cfg)

    def ask(self, question: str, k: int | None = None) -> Result:
        retrieved = self.retriever.retrieve(question, k=k)
        answer = self.generator.generate(question, retrieved)
        report = validate_answer(answer, retrieved)
        return Result(question=question, answer=answer, retrieved=retrieved, validation=report)
