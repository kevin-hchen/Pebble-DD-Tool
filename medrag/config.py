"""Central configuration, loaded from environment with sane defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional dependency
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a convenience, not a requirement
    pass


@dataclass
class Config:
    # --- models ---
    # provider: none | groq | cerebras | openrouter | ollama | openai
    # Defaults to "none" so a fresh install runs free and offline-safe; the
    # setup screen or MEDRAG_PROVIDER picks a real one.
    provider: str = "none"
    embed_model: str = "text-embedding-3-small"
    chat_model: str = "gpt-4o-mini"
    chat_model_explicit: bool = False   # True when the user pinned a model name
    embed_backend: str = "auto"  # auto | openai | sentence-transformers | hashing
    local_embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    # --- chunking ---
    chunk_size: int = 900          # characters, approx. 220 tokens
    chunk_overlap: int = 150
    min_chunk_size: int = 120

    # --- retrieval ---
    top_k: int = 6
    fetch_k: int = 24              # candidates pulled before MMR re-ranking
    mmr_lambda: float = 0.6        # 1.0 = pure relevance, 0.0 = pure diversity
    # The similarity below which a passage is NOT evidence.
    #
    # 0.05 meant "not orthogonal", which admits everything: an asset with no
    # published data returned the k nearest things in the corpus — penicillin
    # and pneumonia papers for a hidradenitis drug — graded, PMID-linked, and
    # presented as its evidence, with the memo reporting "sections answered with
    # evidence: 11/11".
    #
    # MEASURED (2026-08-11, all-MiniLM-L6-v2, the real 820-chunk index) by
    # driving the REAL rendered question set — 11 questions x 33 asset/indication
    # pairs, 363 retrievals — across three disease families the corpus covers
    # (colorectal/MSS, pneumonia/penicillin, neonatal jaundice) and 20 assets it
    # covers not at all. Top-1 cosine per question:
    #
    #                 min    p05    p25    med    p75    p95    p99    max
    #   on-topic     0.334  0.436  0.530  0.643  0.742  0.829  0.897  0.897
    #   off-topic    0.177  0.234  0.311  0.361  0.415  0.482  0.521  0.555
    #
    # THE TWO DISTRIBUTIONS OVERLAP, and an earlier version of this comment —
    # written from a single query pair — claimed they "separate cleanly". They
    # do not: 45 of 143 on-topic scores fall below the highest off-topic one.
    # There is no threshold that admits all real evidence and no false evidence,
    # so this number chooses which error to make, and chooses the one this tool
    # is for. A memo that cites a bilirubinometry meta-analysis as a hidradenitis
    # drug's efficacy evidence is worse than a memo with an empty section, the
    # same way an invented contradiction is worse than silence.
    #
    # Sections retaining literature evidence, of 11:
    #
    #   floor   on-topic   off-topic   off-topic assets left fully silent
    #    0.05     100%        100%           0 of 20
    #    0.35      99%         57%           0 of 20     <- still evidenced everything
    #    0.45      85%          8%          13 of 20
    #    0.50      79%          2%          17 of 20     <- here
    #    0.55      70%          1%          19 of 20
    #    0.60      62%          0%          20 of 20
    #
    # 0.50 sits above the off-topic p95 (0.482) and below the point where the
    # cost lands on evidence that matters. Where the on-topic loss falls is what
    # decided it, not the headline percentage: at 0.50 the eight questions whose
    # answers live in a published abstract — efficacy, endpoints, comparator,
    # evidence quality, terminated trials, bear case — retain 12 or 13 of 13
    # on-topic assets each. Every point of the loss is concentrated in
    # `mechanism` (7/13), `development-stage` (6/13) and `competitive-trials`
    # (0/13), which are registry and regulatory questions that a literature
    # search answers badly at any floor.
    #
    # `competitive-trials` is worth naming separately: its text interpolates
    # NEITHER {asset} NOR {indication}, so it embeds to one fixed vector and
    # scores an identical 0.436 for every asset in the sample, on-topic and
    # off-topic alike. No floor can distinguish anything there, because the
    # query contains nothing to distinguish. It is answered from the registry,
    # and dropping its literature is losing a result that was never about the
    # asset. Fixing the question text is a question-set change, not a code one —
    # config/diligence_questions.yaml.
    #
    # NOT tuned to clear the one asset that exposed this. 0.45 would have zeroed
    # PBX-7749 while leaving 7 of the other 20 absent assets evidenced; picking
    # it would be the retrieval equivalent of tuning the biomarker matcher until
    # recall hit six.
    #
    # STATED PLAINLY: this number is calibrated to ONE embedder and ONE corpus.
    # A different embedding model has a different cosine scale and this must be
    # re-measured, not assumed to transfer. Re-measure with
    # tests/test_retrieval_relevance.py, which pins the property rather than the
    # number. MEDRAG_SCORE_FLOOR overrides it.
    score_floor: float = 0.50

    # --- evidence grading ---
    # 0.0 disables tier reranking entirely; the eval harness toggles this to
    # measure recall before and after.
    evidence_weight: float = 1.0

    # --- generation ---
    temperature: float = 0.0
    max_context_chars: int = 12000

    # --- fda ---
    # How many 510(k) clearances a diligence section shows. A category can hold
    # hundreds; the memo states this as "N of M" so the cap is never mistaken for
    # the whole category. Configurable via MEDRAG_FDA_MAX_CLEARANCES.
    fda_max_clearances: int = 25

    # --- storage ---
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("MEDRAG_DATA_DIR", "data")))

    # --- external services ---
    openai_api_key: str | None = None
    ncbi_email: str | None = None
    ncbi_api_key: str | None = None

    # --- privacy / security ---
    encrypt: bool = False       # encrypt corpus and index at rest
    offline: bool = False       # hard-block every outbound call
    # The public-deployment switch. STRICTLY STRONGER than offline: offline
    # blocks outbound calls but still lets the process write its own database,
    # corpus, index and .env. read_only additionally forbids all of that, so the
    # app can serve a snapshot from a filesystem it has no permission to change.
    #
    # It is a separate flag rather than a mode inferred from a read-only mount,
    # because a mount that happens to be writable must not silently re-enable
    # fetching and writing — a deployment's guarantees should not depend on how
    # the volume was mounted that day.
    read_only: bool = False
    passphrase: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        """Never let a key or passphrase reach a log line or traceback."""
        return (
            f"Config(embed_model={self.embed_model!r}, chat_model={self.chat_model!r}, "
            f"embed_backend={self.embed_backend!r}, data_dir={str(self.data_dir)!r}, "
            f"encrypt={self.encrypt}, offline={self.offline}, "
            f"read_only={self.read_only}, "
            f"openai_api_key={'set' if self.openai_api_key else 'unset'}, "
            f"passphrase={'set' if self.passphrase else 'unset'})"
        )

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    def ensure_dirs(self) -> None:
        """Create the data directories — a no-op in read-only mode.

        Every Streamlit page called this unconditionally at module scope, and
        the `mkdir` was not wrapped, so on a read-only filesystem the page died
        at import before rendering a single element. In read-only mode the
        directories either already exist (they hold the snapshot being served)
        or the store open will fail with a message naming the missing file,
        which is a better error than an OSError out of an import.
        """
        if self.read_only:
            return
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        for d in (self.data_dir, self.raw_dir, self.index_dir):
            try:
                os.chmod(d, 0o700)
            except OSError:  # pragma: no cover - non-POSIX filesystems
                pass


def load_config() -> Config:
    provider = os.getenv("MEDRAG_PROVIDER", Config.provider).lower()

    cfg = Config(
        provider=provider,
        embed_model=os.getenv("MEDRAG_EMBED_MODEL", Config.embed_model),
        chat_model=os.getenv("MEDRAG_CHAT_MODEL", Config.chat_model),
        chat_model_explicit=bool(os.getenv("MEDRAG_CHAT_MODEL")),
        embed_backend=os.getenv("MEDRAG_EMBED_BACKEND", Config.embed_backend).lower(),
        data_dir=Path(os.getenv("MEDRAG_DATA_DIR", "data")),
        openai_api_key=_provider_key(provider),
        ncbi_email=os.getenv("NCBI_EMAIL") or None,
        ncbi_api_key=os.getenv("NCBI_API_KEY") or None,
    )
    # The model name follows the provider unless explicitly pinned, so switching
    # provider does not silently keep asking a different vendor for gpt-4o-mini.
    from .providers import resolve_model

    cfg.chat_model = resolve_model(cfg)
    if os.getenv("MEDRAG_TOP_K"):
        cfg.top_k = int(os.environ["MEDRAG_TOP_K"])
    if os.getenv("MEDRAG_FDA_MAX_CLEARANCES"):
        cfg.fda_max_clearances = int(os.environ["MEDRAG_FDA_MAX_CLEARANCES"])

    cfg.encrypt = _truthy(os.getenv("MEDRAG_ENCRYPT"))
    cfg.offline = _truthy(os.getenv("MEDRAG_OFFLINE"))
    # Read-only IMPLIES offline, and the implication is one-directional and
    # enforced here rather than remembered at each call site. A public reader
    # that could still fetch would let a stranger's search trigger a registry
    # pull from the server — the counts would move under other visitors, the
    # server would carry the traffic, and the fetch would try to write.
    cfg.read_only = _truthy(os.getenv("MEDRAG_READ_ONLY"))
    if cfg.read_only:
        cfg.offline = True
    if cfg.offline:
        # Offline means offline: drop the key so no code path can transmit.
        cfg.openai_api_key = None
    return cfg


def _truthy(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _provider_key(provider: str) -> str | None:
    """Read the key for the active provider.

    Each provider has its own env var so several can be configured side by side
    and switching provider does not send one vendor's key to another.
    OPENAI_API_KEY is accepted as a fallback for backwards compatibility.
    """
    from .providers import get_provider

    var = get_provider(provider).env_var
    return os.getenv(var) or os.getenv("OPENAI_API_KEY") or None
