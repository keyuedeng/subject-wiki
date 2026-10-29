"""Hybrid search over the in-memory backend: fusion, single modes and filters."""

import pytest
from sqlalchemy import select

from app.db.models import Chunk
from app.search.backend import Filters
from app.search.search import Searcher
from tests.search_fixtures import FakeQAProvider, MemoryBackend, build_subject, make_indexer
from tests.transcript_fixtures import bag_of_words


@pytest.fixture
def indexed(db, session_factory, subject):
    files = build_subject(db, subject)
    indexer = make_indexer(session_factory, FakeQAProvider())
    for f in files:
        indexer.index_file(f.id)
    return files


@pytest.fixture
def searcher():
    return Searcher(MemoryBackend(), bag_of_words)


def labels(db, hits) -> list[tuple[str, int]]:
    out = []
    for h in hits:
        c = db.get(Chunk, h.chunk_id)
        out.append((c.source_type, c.position))
    return out


def test_hybrid_puts_chunks_both_methods_agree_on_first(db, subject, indexed, searcher):
    result = searcher.search(db, subject.id, "Who creates instances of an aggregated class?")
    top = result.hits[0]
    assert labels(db, [top]) == [("slide", 1)]  # the Creator slide
    assert top.keyword_rank is not None and top.embedding_rank is not None
    assert [h.rank for h in result.hits] == list(range(1, len(result.hits) + 1))
    assert result.top_similarity and result.top_similarity > 0.2


def test_single_modes_return_one_list(db, subject, indexed, searcher):
    keyword = searcher.search(db, subject.id, "guard condition", mode="keyword")
    assert all(h.embedding_rank is None for h in keyword.hits)
    assert labels(db, keyword.hits[:2]) == [("slide", 1), ("slide", 2)]  # week 6 deck
    embedding = searcher.search(db, subject.id, "guard condition", mode="embedding")
    assert all(h.keyword_rank is None for h in embedding.hits)
    assert embedding.top_similarity == embedding.hits[0].score


def test_filters_by_week_and_source_type(db, subject, indexed, searcher):
    grasp, vtt, states = indexed
    weeks = searcher.search(db, subject.id, "responsibility class", Filters(weeks=(6,)), k=20)
    files = {db.get(Chunk, h.chunk_id).file_id for h in weeks.hits}
    assert files == {states.id}
    only_said = searcher.search(db, subject.id, "creator", Filters(types=("transcript",)), k=20)
    assert {db.get(Chunk, h.chunk_id).source_type for h in only_said.hits} == {"transcript"}
    notes = searcher.search(db, subject.id, "controller UI", Filters(types=("notes",)))
    assert labels(db, notes.hits) == [("notes", 4)]


def test_excluded_and_retyped_files_drop_out_without_reindexing(db, subject, indexed, searcher):
    grasp, vtt, states = indexed
    grasp.included = False
    states.type = "other"
    db.commit()
    hits = searcher.search(db, subject.id, "guard creator expert controller", k=50).hits
    assert {db.get(Chunk, h.chunk_id).file_id for h in hits} == {vtt.id}
    assert db.scalar(select(Chunk).where(Chunk.file_id == grasp.id)) is not None  # still stored
