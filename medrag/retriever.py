"""Retrieval with MMR re-ranking and per-document diversity.

Plain top-k on a PubMed corpus tends to return five chunks of the same trial.
Maximal Marginal Relevance trades a little relevance for coverage, which matters
when the answer needs to reflect more than one study.
"""

from __future__ import annotations

import numpy as np

from .config import Config
from .documents import Retrieved
from .embeddings import Embedder
from .evidence_grade import rerank_by_evidence
from .vectorstore import VectorStore


def mmr_select(
    query_vec: np.ndarray,
    cand_vecs: np.ndarray,
    k: int,
    lambda_mult: float = 0.6,
) -> list[int]:
    """Select k candidate indices maximizing relevance minus redundancy."""
    if len(cand_vecs) == 0:
        return []
    k = min(k, len(cand_vecs))
    relevance = cand_vecs @ query_vec           # vectors are normalized
    similarity = cand_vecs @ cand_vecs.T

    selected = [int(np.argmax(relevance))]
    while len(selected) < k:
        redundancy = similarity[:, selected].max(axis=1)
        score = lambda_mult * relevance - (1 - lambda_mult) * redundancy
        score[selected] = -np.inf
        selected.append(int(np.argmax(score)))
    return selected


class Retriever:
    def __init__(self, store: VectorStore, embedder: Embedder, cfg: Config):
        self.store = store
        self.embedder = embedder
        self.cfg = cfg
        if store.embedder_name not in ("unknown", embedder.name):
            raise RuntimeError(
                f"index was built with '{store.embedder_name}' but the active embedder is "
                f"'{embedder.name}'. Re-run `medrag index` to rebuild."
            )
        # Refuse an index that predates the funding-disclosure fields, the same way
        # a mismatched embedder is refused: it would answer the independence axis
        # from stale data (every source reading NO DISCLOSURE) without saying so.
        from .vectorstore import INDEX_SCHEMA

        if store.index_schema and store.index_schema != INDEX_SCHEMA:
            raise RuntimeError(
                f"index schema '{store.index_schema}' is out of date (current is "
                f"'{INDEX_SCHEMA}'). Re-run `medrag index` to rebuild."
            )
        if not store.index_schema:
            raise RuntimeError(
                "this index was built before funding-disclosure tracking was added, "
                "so the independence check cannot run against it. Re-run "
                "`medrag index` to rebuild it."
            )

    def retrieve(
        self,
        query: str,
        k: int | None = None,
        use_mmr: bool = True,
        max_per_doc: int = 2,
        evidence_weight: float | None = None,
    ) -> list[Retrieved]:
        k = k or self.cfg.top_k
        qvec = self.embedder.embed_query(query)

        candidates = self.store.search(qvec, max(self.cfg.fetch_k, k))
        candidates = [c for c in candidates if c.score >= self.cfg.score_floor]
        if not candidates:
            return []

        if use_mmr and len(candidates) > k:
            id_to_pos = {c.chunk_id: i for i, c in enumerate(self.store.chunks)}
            positions = [id_to_pos[r.chunk.chunk_id] for r in candidates]
            vecs = self.store.vectors_for(positions)
            order = mmr_select(qvec, vecs, k=len(candidates), lambda_mult=self.cfg.mmr_lambda)
            candidates = [candidates[i] for i in order]

        # Evidence-tier reranking, before the per-document cap so a stronger
        # design can win the slot. Gentle by design: an off-topic meta-analysis
        # should still lose to an on-topic cohort study.
        weight = self.cfg.evidence_weight if evidence_weight is None else evidence_weight
        candidates = rerank_by_evidence(candidates, weight=weight)

        # Cap chunks per source document so one paper can't crowd out the rest.
        picked: list[Retrieved] = []
        seen: dict[str, int] = {}
        for r in candidates:
            if seen.get(r.chunk.doc_id, 0) >= max_per_doc:
                continue
            seen[r.chunk.doc_id] = seen.get(r.chunk.doc_id, 0) + 1
            picked.append(r)
            if len(picked) == k:
                break

        return picked
