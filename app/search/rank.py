"""Ranking, pure: BM25 over term statistics, and reciprocal rank fusion.

Postgres full-text search finds the candidate chunks and their term counts (title terms
count twice), but its own ts_rank has no inverse document frequency, so a question's
common words ("pattern", "class") would swamp its rare ones. BM25 is computed here
instead, from the counts the backend returns.
"""

import math
from collections.abc import Hashable, Sequence
from dataclasses import dataclass, field

K1 = 1.2
B = 0.75
RRF_K = 60


@dataclass
class TermStats:
    """Counts over the (filtered) chunks of one subject."""

    n_docs: int
    avg_len: float
    # chunk id -> (document length in lexemes, {term: weighted count})
    postings: dict[Hashable, tuple[int, dict[str, int]]] = field(default_factory=dict)

    def df(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, terms in self.postings.values():
            for t in terms:
                out[t] = out.get(t, 0) + 1
        return out


def bm25(stats: TermStats, k1: float = K1, b: float = B) -> list[tuple[Hashable, float]]:
    """Chunks that contain at least one query term, best first (ties by id)."""
    if not stats.postings or stats.n_docs == 0:
        return []
    df = stats.df()
    n, avg = stats.n_docs, max(stats.avg_len, 1.0)
    idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
    scores = []
    for cid, (length, terms) in stats.postings.items():
        norm = k1 * (1 - b + b * length / avg)
        score = sum(idf[t] * tf * (k1 + 1) / (tf + norm) for t, tf in terms.items())
        scores.append((cid, score))
    scores.sort(key=lambda x: (-x[1], str(x[0])))
    return scores


def rrf(rankings: Sequence[Sequence[Hashable]], k: int = RRF_K) -> list[tuple[Hashable, float]]:
    """Reciprocal rank fusion: score = sum of 1 / (k + rank) over the lists an item is
    in. Ties go to the item with the better single rank, then by id."""
    scores: dict[Hashable, float] = {}
    best: dict[Hashable, int] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
            best[item] = min(best.get(item, rank), rank)
    return sorted(scores.items(), key=lambda x: (-x[1], best[x[0]], str(x[0])))
