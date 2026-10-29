"""Search backends: what search needs from the database, and the Postgres implementation.

Postgres does the heavy lifting with its indexes: the GIN index on `chunks.tsv` finds
chunks containing any query term (and returns their term counts for BM25), and the HNSW
index on `chunks.embedding` finds nearest neighbours. Tests use an in-memory backend with
the same interface (tests/search_fixtures.py).

Every query reads week, type and `included` from the file, so board edits apply at once.
"""

import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.search.index import INDEXED_TYPES
from app.search.rank import TermStats


@dataclass(frozen=True)
class Filters:
    weeks: tuple[int, ...] = ()
    types: tuple[str, ...] = ()  # chunk source types: slide, notes, transcript, tutorial


NO_FILTERS = Filters()


class SearchBackend(Protocol):
    def query_terms(self, db: Session, query: str) -> list[str]:
        """The query's searchable terms (stemmed, no stop words)."""
        ...

    def term_stats(
        self, db: Session, subject_id: uuid.UUID, terms: list[str], filters: Filters
    ) -> TermStats: ...

    def nearest(
        self, db: Session, subject_id: uuid.UUID, vector: list[float], filters: Filters, limit: int
    ) -> list[tuple[uuid.UUID, float]]:
        """(chunk id, cosine similarity), most similar first."""
        ...

    def phrase_matches(
        self, db: Session, subject_id: uuid.UUID, phrase: str, filters: Filters, limit: int
    ) -> list[uuid.UUID]:
        """Chunks containing the phrase (stemmed, words in order)."""
        ...


def _where(subject_id: uuid.UUID, filters: Filters) -> tuple[str, dict]:
    sql = "c.subject_id = :subject AND f.included AND f.type = ANY(:file_types)"
    params: dict = {"subject": subject_id, "file_types": list(INDEXED_TYPES)}
    if filters.weeks:
        sql += " AND f.week = ANY(:weeks)"
        params["weeks"] = list(filters.weeks)
    if filters.types:
        sql += " AND c.source_type = ANY(:types)"
        params["types"] = list(filters.types)
    return sql, params


_FROM = "chunks c JOIN files f ON f.id = c.file_id"
# Any of the terms (OR): a question's words rarely all appear in one chunk.
_ANY_TERM = (
    "array_to_string(ARRAY(SELECT quote_literal(t) FROM unnest(CAST(:terms AS text[])) t), "
    "' | ')::tsquery"
)


class PgBackend:
    def query_terms(self, db: Session, query: str) -> list[str]:
        rows = db.execute(
            text("SELECT lexeme FROM unnest(to_tsvector('english', :q))"), {"q": query}
        )
        return [r[0] for r in rows]

    def term_stats(
        self, db: Session, subject_id: uuid.UUID, terms: list[str], filters: Filters
    ) -> TermStats:
        where, params = _where(subject_id, filters)
        n, avg = db.execute(
            text(f"SELECT count(*), coalesce(avg(length(c.tsv)), 0) FROM {_FROM} WHERE {where}"),
            params,
        ).one()
        stats = TermStats(int(n), float(avg))
        if not terms or not n:
            return stats
        # Weighted count: title (A) occurrences count twice.
        rows = db.execute(
            text(
                f"SELECT c.id, length(c.tsv), u.lexeme, "
                f"(SELECT count(*) + count(*) FILTER (WHERE w = 'A') FROM unnest(u.weights) w) "
                f"FROM {_FROM} CROSS JOIN LATERAL unnest(c.tsv) u "
                f"WHERE {where} AND c.tsv @@ {_ANY_TERM} AND u.lexeme = ANY(:terms)"
            ),
            {**params, "terms": terms},
        )
        for cid, length, lexeme, tf in rows:
            stats.postings.setdefault(cid, (int(length), {}))[1][lexeme] = int(tf)
        return stats

    def nearest(
        self, db: Session, subject_id: uuid.UUID, vector: list[float], filters: Filters, limit: int
    ) -> list[tuple[uuid.UUID, float]]:
        where, params = _where(subject_id, filters)
        # Keep scanning the HNSW graph until enough rows pass the subject/week filters.
        db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        rows = db.execute(
            text(
                f"SELECT c.id, c.embedding <=> CAST(:v AS vector) AS dist FROM {_FROM} "
                f"WHERE {where} ORDER BY dist LIMIT :limit"
            ),
            {**params, "v": "[" + ",".join(f"{x:.7g}" for x in vector) + "]", "limit": limit},
        )
        # relaxed_order can return near-ties slightly out of order.
        return sorted(((cid, 1.0 - float(d)) for cid, d in rows), key=lambda x: -x[1])

    def phrase_matches(
        self, db: Session, subject_id: uuid.UUID, phrase: str, filters: Filters, limit: int
    ) -> list[uuid.UUID]:
        where, params = _where(subject_id, filters)
        rows = db.execute(
            text(
                f"SELECT c.id FROM {_FROM}, phraseto_tsquery('english', :p) q "
                f"WHERE {where} AND numnode(q) > 0 AND c.tsv @@ q "
                f"ORDER BY ts_rank_cd(c.tsv, q) DESC LIMIT :limit"
            ),
            {**params, "p": phrase, "limit": limit},
        )
        return [r[0] for r in rows]
