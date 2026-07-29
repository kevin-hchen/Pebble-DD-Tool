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
    score_floor: float = 0.05      # drop near-orthogonal matches

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
    passphrase: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        """Never let a key or passphrase reach a log line or traceback."""
        return (
            f"Config(embed_model={self.embed_model!r}, chat_model={self.chat_model!r}, "
            f"embed_backend={self.embed_backend!r}, data_dir={str(self.data_dir)!r}, "
            f"encrypt={self.encrypt}, offline={self.offline}, "
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
