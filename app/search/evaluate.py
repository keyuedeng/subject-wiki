"""Retrieval accuracy on a question set. Pure.

Each question has the chunk it was written from (and any others a person marked as
fine). A hit is any of those in the top k. "Relaxed" also accepts another chunk about the
same slide (its lecturer notes, or a lecture moment aligned to it): a student sent there
still lands on the right slide.
"""

from dataclasses import dataclass, field

KS = (1, 5, 10)


@dataclass
class Case:
    key: str  # chunk id the question was written from
    also_ok: frozenset[str] = frozenset()
    slide_id: str | None = None  # the source's slide, for relaxed matching
    group: str = ""  # e.g. the source type, for a breakdown


@dataclass
class Retrieved:
    chunk_id: str
    slide_id: str | None = None


def first_hit(case: Case, results: list[Retrieved], relaxed: bool = False) -> int | None:
    """1-based rank of the first acceptable result."""
    ok = {case.key} | case.also_ok
    for rank, r in enumerate(results, 1):
        if r.chunk_id in ok or (relaxed and case.slide_id and r.slide_id == case.slide_id):
            return rank
    return None


@dataclass
class Scores:
    n: int = 0
    hits: dict[int, int] = field(default_factory=lambda: dict.fromkeys(KS, 0))
    rr: float = 0.0

    def add(self, rank: int | None) -> None:
        self.n += 1
        for k in KS:
            self.hits[k] += rank is not None and rank <= k
        self.rr += 1 / rank if rank else 0.0

    def recall(self, k: int) -> float:
        return self.hits[k] / self.n if self.n else 0.0

    @property
    def mrr(self) -> float:
        return self.rr / self.n if self.n else 0.0
