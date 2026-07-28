"""Embedding backends with graceful degradation.

Resolution order under MEDRAG_EMBED_BACKEND=auto:
  1. OpenAI  (text-embedding-3-small) - used whenever OPENAI_API_KEY is set
  2. sentence-transformers (all-MiniLM-L6-v2) - local, no key, needs torch
  3. hashing - dependency-free character n-gram projection; always available

The fallbacks exist so the pipeline is demonstrable offline. Backend identity is
written into the index manifest, and a query embedded by a different backend than
the index is refused rather than silently returning nonsense.
"""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod

import numpy as np

from .config import Config


def l2_normalize(mat: np.ndarray) -> np.ndarray:
    """Normalize rows so inner product == cosine similarity."""
    mat = np.asarray(mat, dtype="float32")
    if mat.ndim == 1:
        mat = mat.reshape(1, -1)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return mat / np.maximum(norms, 1e-10)


class Embedder(ABC):
    name: str
    dim: int

    @abstractmethod
    def _embed(self, texts: list[str]) -> np.ndarray: ...

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype="float32")
        return l2_normalize(self._embed(texts))

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed([text])[0]


class OpenAIEmbedder(Embedder):
    def __init__(self, cfg: Config):
        from openai import OpenAI

        self.client = OpenAI(api_key=cfg.openai_api_key)
        self.model = cfg.embed_model
        self.name = f"openai:{self.model}"
        self.dim = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}.get(
            self.model, 1536
        )

    def _embed(self, texts: list[str]) -> np.ndarray:
        vectors: list[list[float]] = []
        batch = 96  # keeps each request well inside the token limit
        for i in range(0, len(texts), batch):
            resp = self.client.embeddings.create(
                model=self.model, input=[t.replace("\n", " ") for t in texts[i : i + batch]]
            )
            vectors.extend(d.embedding for d in resp.data)
        return np.array(vectors, dtype="float32")


class SentenceTransformerEmbedder(Embedder):
    def __init__(self, cfg: Config):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(cfg.local_embed_model)
        self.name = f"sentence-transformers:{cfg.local_embed_model}"
        self.dim = self.model.get_sentence_embedding_dimension()

    def _embed(self, texts: list[str]) -> np.ndarray:
        return np.asarray(
            self.model.encode(texts, batch_size=32, show_progress_bar=False), dtype="float32"
        )


class HashingEmbedder(Embedder):
    """Dependency-free fallback: hashed word + character-trigram features.

    Not competitive with a trained model, but deterministic, instant, and enough
    to exercise (and test) the full retrieval path with no network or GPU.
    """

    def __init__(self, dim: int = 1024):
        self.dim = dim
        self.name = f"hashing:{dim}"

    @staticmethod
    def _tokens(text: str) -> list[str]:
        words = re.findall(r"[a-z0-9]+", text.lower())
        feats = list(words)
        feats += [f"{a}_{b}" for a, b in zip(words, words[1:])]  # bigrams
        for w in words:
            if len(w) > 4:  # char trigrams give morphological robustness
                feats += [f"#{w[i:i+3]}" for i in range(len(w) - 2)]
        return feats

    def _embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype="float32")
        for row, text in enumerate(texts):
            for tok in self._tokens(text):
                h = int(hashlib.md5(tok.encode()).hexdigest()[:8], 16)
                sign = 1.0 if h & 1 else -1.0  # signed hashing limits collision bias
                out[row, h % self.dim] += sign
        return np.sign(out) * np.log1p(np.abs(out))  # damp frequent-term dominance


def get_embedder(cfg: Config, verbose: bool = True) -> Embedder:
    backend = (cfg.embed_backend or "auto").lower()

    def _log(msg: str) -> None:
        if verbose:
            print(f"[medrag] {msg}")

    offline = getattr(cfg, "offline", False)
    if offline:
        if backend == "openai":
            raise RuntimeError("MEDRAG_OFFLINE=1 conflicts with MEDRAG_EMBED_BACKEND=openai")
        _log("offline mode: local embeddings only, nothing transmitted")

    # Only OpenAI serves embeddings among the configured providers. On a free
    # provider the key is for chat, so sending it to an embeddings endpoint that
    # does not exist would fail confusingly - go local instead.
    from .providers import effective_provider

    provider = effective_provider(cfg)
    can_use_openai_embeddings = provider.key == "openai" and provider.serves_embeddings

    if backend == "auto" and not can_use_openai_embeddings:
        backend = "local-first"

    if not offline and backend in ("openai", "auto") and cfg.openai_api_key:
        try:
            emb = OpenAIEmbedder(cfg)
            _log(f"embeddings: {emb.name}")
            return emb
        except Exception as exc:
            if backend == "openai":
                raise
            _log(f"OpenAI embeddings unavailable ({exc}); falling back")
    elif backend == "openai":
        raise RuntimeError("MEDRAG_EMBED_BACKEND=openai but OPENAI_API_KEY is not set")

    if backend in ("sentence-transformers", "auto", "local-first"):
        try:
            emb = SentenceTransformerEmbedder(cfg)
            _log(f"embeddings: {emb.name} (local, free, nothing transmitted)")
            return emb
        except Exception as exc:
            if backend == "sentence-transformers":
                raise
            reason = str(exc)
            if "No module named" in reason:
                _log(
                    "sentence-transformers is not installed, so retrieval is running on "
                    "the weak built-in fallback. For much better results at no cost, run: "
                    "pip install -r requirements-offline.txt"
                )
            else:
                _log(f"local embedding model unavailable ({reason}); falling back to hashing")

    emb = HashingEmbedder()
    _log(f"embeddings: {emb.name} (last-resort fallback - retrieval quality is degraded)")
    return emb
