"""FAISS-backed vector store with on-disk persistence.

Vectors are L2-normalized, so IndexFlatIP gives exact cosine similarity. Flat is
the right call at this corpus size (tens of thousands of chunks): exact search,
no training step, no recall cliff. Swap in IVF/HNSW past ~1M vectors.

If faiss isn't installed the store falls back to a NumPy matmul that returns
identical results - slower, but keeps the project runnable anywhere.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np

from .crypto import harden_permissions, read_secure, write_secure
from .documents import Chunk, Retrieved
from .jsonl import dumps_line, split_lines

try:
    import faiss

    HAS_FAISS = True
except ImportError:  # pragma: no cover
    HAS_FAISS = False

# Bumped whenever a chunk field the pipeline depends on is added. An index built
# before the field exists cannot answer the question the field was added for, so
# it is refused with a rebuild instruction rather than silently degraded — the
# same contract as the embedder-name check. "disclosure-v1" adds the per-chunk
# funder/COI signal the independence axis reads.
INDEX_SCHEMA = "disclosure-v1"


class VectorStore:
    def __init__(self, dim: int, embedder_name: str = "unknown",
                 index_schema: str = INDEX_SCHEMA):
        self.dim = dim
        self.embedder_name = embedder_name
        # A freshly built store is current by construction; only load() sets this
        # from a manifest, where an older or absent value signals a stale index.
        self.index_schema = index_schema
        self.chunks: list[Chunk] = []
        if HAS_FAISS:
            self.index = faiss.IndexFlatIP(dim)
            self._matrix = None
        else:
            self.index = None
            self._matrix = np.zeros((0, dim), dtype="float32")

    def __len__(self) -> int:
        return len(self.chunks)

    def add(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        if len(chunks) != len(vectors):
            raise ValueError(f"chunk/vector count mismatch: {len(chunks)} vs {len(vectors)}")
        if len(chunks) == 0:
            return
        vectors = np.ascontiguousarray(vectors, dtype="float32")
        if vectors.shape[1] != self.dim:
            raise ValueError(f"expected dim {self.dim}, got {vectors.shape[1]}")
        if HAS_FAISS:
            self.index.add(vectors)
        else:
            self._matrix = np.vstack([self._matrix, vectors])
        self.chunks.extend(chunks)

    def search(self, query_vec: np.ndarray, k: int) -> list[Retrieved]:
        if len(self.chunks) == 0:
            return []
        k = min(k, len(self.chunks))
        q = np.ascontiguousarray(query_vec, dtype="float32").reshape(1, -1)

        if HAS_FAISS:
            scores, idx = self.index.search(q, k)
            scores, idx = scores[0], idx[0]
        else:
            sims = (self._matrix @ q.T).ravel()
            idx = np.argsort(-sims)[:k]
            scores = sims[idx]

        return [
            Retrieved(chunk=self.chunks[i], score=float(s))
            for s, i in zip(scores, idx)
            if i != -1
        ]

    def vectors_for(self, indices: list[int]) -> np.ndarray:
        """Reconstruct stored vectors (needed for MMR re-ranking)."""
        if HAS_FAISS:
            return np.vstack([self.index.reconstruct(int(i)) for i in indices])
        return self._matrix[indices]

    # --- persistence ---
    #
    # Vectors and chunk text are written through medrag.crypto, so both are
    # encrypted when a passphrase is configured. The manifest deliberately stays
    # in the clear: it holds only dimensionality, embedder name and chunk count,
    # and reading it is what tells us whether a passphrase is needed at all.
    # Note that embeddings are NOT a privacy boundary - inversion attacks can
    # partially reconstruct source text - so they are encrypted alongside it.

    def save(self, directory: str | Path, passphrase: str | None = None) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        harden_permissions(directory)

        if HAS_FAISS:
            payload = faiss.serialize_index(self.index).tobytes()
            write_secure(directory / "index.faiss", payload, passphrase)
        else:
            buf = io.BytesIO()
            np.save(buf, self._matrix)
            write_secure(directory / "vectors.npy", buf.getvalue(), passphrase)

        chunk_text = "\n".join(dumps_line(c.to_dict()) for c in self.chunks)
        write_secure(directory / "chunks.jsonl", chunk_text.encode("utf-8"), passphrase)

        manifest = {
            "dim": self.dim,
            "embedder": self.embedder_name,
            "schema": self.index_schema,
            "n_chunks": len(self.chunks),
            "backend": "faiss" if HAS_FAISS else "numpy",
            "encrypted": bool(passphrase),
        }
        write_secure(
            directory / "manifest.json",
            json.dumps(manifest, indent=2).encode(),
            None,
            allow_plaintext=True,  # intentionally public: tells readers a key is needed
        )
        return directory

    @classmethod
    def load(cls, directory: str | Path, passphrase: str | None = None) -> "VectorStore":
        directory = Path(directory)
        manifest = json.loads((directory / "manifest.json").read_text())
        store = cls(
            dim=manifest["dim"],
            embedder_name=manifest.get("embedder", "unknown"),
            # Absent on indexes built before schema tracking, which is the signal
            # that they predate the disclosure fields.
            index_schema=manifest.get("schema", ""),
        )

        faiss_path, npy_path = directory / "index.faiss", directory / "vectors.npy"
        if HAS_FAISS and faiss_path.exists():
            raw = read_secure(faiss_path, passphrase)
            store.index = faiss.deserialize_index(np.frombuffer(raw, dtype="uint8"))
        elif npy_path.exists():
            vectors = np.load(io.BytesIO(read_secure(npy_path, passphrase)))
            if HAS_FAISS:
                store.index = faiss.IndexFlatIP(store.dim)
                store.index.add(np.ascontiguousarray(vectors, dtype="float32"))
            else:
                store._matrix = vectors
        elif faiss_path.exists() and not HAS_FAISS:
            raise RuntimeError("index was built with faiss but faiss is not installed")
        else:
            raise FileNotFoundError(f"no vector data found in {directory}")

        text = read_secure(directory / "chunks.jsonl", passphrase).decode("utf-8")
        store.chunks = [Chunk.from_dict(json.loads(l)) for l in split_lines(text)]
        return store
