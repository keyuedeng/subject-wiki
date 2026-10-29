"""Hybrid search: BM25 keyword results and embedding results, combined with reciprocal
rank fusion. `mode` picks one list alone (for the eval) or both."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.orm import Session

from app.search.backend import NO_FILTERS, Filters, SearchBackend
from app.search.rank import bm25, rrf

DEPTH = 50  # results taken from each list before fusing
Mode = Literal["hybrid", "keyword", "embedding"]


@dataclass
class Hit:
    chunk_id: uuid.UUID
    rank: int  # 1-based, in the returned order
    score: float  # RRF score (hybrid), BM25 (keyword) or cosine similarity (embedding)
    keyword_rank: int | None = None
    embedding_rank: int | None = None
    similarity: float | None = None


@dataclass
class SearchResult:
    hits: list[Hit]
    terms: list[str] = field(default_factory=list)
    top_similarity: float | None = None  # best cosine similarity among all candidates


@dataclass
class Searcher:
    backend: SearchBackend
    embed: Callable[[str], list[float]]  # cached query embedding
    depth: int = DEPTH

    def search(
        self,
        db: Session,
        subject_id: uuid.UUID,
        query: str,
        filters: Filters = NO_FILTERS,
        k: int = 8,
        mode: Mode = "hybrid",
    ) -> SearchResult:
        terms: list[str] = []
        keyword: list[tuple[uuid.UUID, float]] = []
        nearest: list[tuple[uuid.UUID, float]] = []
        if mode in ("hybrid", "keyword"):
            terms = self.backend.query_terms(db, query)
            stats = self.backend.term_stats(db, subject_id, terms, filters)
            keyword = bm25(stats)[: self.depth]
        if mode in ("hybrid", "embedding"):
            nearest = self.backend.nearest(db, subject_id, self.embed(query), filters, self.depth)

        kw_rank = {cid: i for i, (cid, _) in enumerate(keyword, 1)}
        emb_rank = {cid: i for i, (cid, _) in enumerate(nearest, 1)}
        sim = dict(nearest)
        if mode == "keyword":
            ordered = keyword
        elif mode == "embedding":
            ordered = nearest
        else:
            ordered = rrf([[c for c, _ in keyword], [c for c, _ in nearest]])
        hits = [
            Hit(cid, i, score, kw_rank.get(cid), emb_rank.get(cid), sim.get(cid))
            for i, (cid, score) in enumerate(ordered[:k], 1)
        ]
        top = nearest[0][1] if nearest else None
        return SearchResult(hits, terms, top)


def fuse(results: list[SearchResult], k: int) -> SearchResult:
    """Combine several searches' hits by reciprocal rank fusion (e.g. the follow-up
    question alone, and with the previous question for context). Each hit keeps the
    ranks from the first search it appeared in."""
    if len(results) == 1:
        return SearchResult(results[0].hits[:k], results[0].terms, results[0].top_similarity)
    first: dict[uuid.UUID, Hit] = {}
    for result in results:
        for h in result.hits:
            first.setdefault(h.chunk_id, h)
    ordered = rrf([[h.chunk_id for h in r.hits] for r in results])
    hits = []
    for i, (cid, score) in enumerate(ordered[:k], 1):
        h = first[cid]
        hits.append(Hit(cid, i, score, h.keyword_rank, h.embedding_rank, h.similarity))
    sims = [r.top_similarity for r in results if r.top_similarity is not None]
    terms = list(dict.fromkeys(t for r in results for t in r.terms))
    return SearchResult(hits, terms, max(sims) if sims else None)
