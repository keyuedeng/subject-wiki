"""The search index: what becomes a chunk, re-indexing only what changed, and indexing
after each job finishes. Embeddings are faked (hashed word counts)."""

from sqlalchemy import select

from app.db.models import Chunk, Slide
from app.jobs.worker import ParseQueue
from app.parse.pipeline import Parser
from app.services.files import delete_file
from app.transcripts.pipeline import TranscriptProcessor, TranscriptSettings
from tests.search_fixtures import FakeQAProvider, build_subject, make_indexer, make_llm
from tests.slide_fixtures import make_deck
from tests.test_pipeline import FakeVision
from tests.test_transcript_pipeline import ORDER, InlineExecutor, upload
from tests.transcript_fixtures import FakeProvider, deck_pages, lecture_vtt


def chunks(db, file=None) -> list[Chunk]:
    db.expire_all()
    q = select(Chunk).order_by(Chunk.source_type, Chunk.position)
    return list(db.scalars(q.where(Chunk.file_id == file.id) if file else q))


def test_kept_slides_notes_and_transcript_segments_become_chunks(db, session_factory, subject):
    grasp, vtt, _ = build_subject(db, subject)
    indexer = make_indexer(session_factory, FakeQAProvider())
    assert indexer.index_file(grasp.id).chunks == 5  # 4 kept slides + 1 notes
    slides = [c for c in chunks(db, grasp) if c.source_type == "slide"]
    assert [c.position for c in slides] == [1, 3, 4, 5]  # the build step (page 2) is skipped
    assert "contains or compositely aggregates" in slides[0].body
    notes = [c for c in chunks(db, grasp) if c.source_type == "notes"]
    assert [(n.position, n.title) for n in notes] == [(4, "GRASP: Controller")]
    assert notes[0].meta["slide_id"] == slides[2].meta["slide_id"]

    indexer.index_file(vtt.id)
    said = chunks(db, vtt)
    assert [c.meta["idx"] for c in said] == [1, 2, 3]  # the admin window is left out
    assert said[0].title == "GRASP: Creator"  # titled with the aligned slide
    assert said[0].meta["flags"] == ["exam_hint"] and said[0].meta["start_ms"] == 45_000
    assert said[2].meta["slide_page"] == 3


def test_reindex_embeds_only_changed_chunks_and_drops_removed_ones(db, session_factory, subject):
    grasp, _, _ = build_subject(db, subject)
    provider = FakeQAProvider()
    indexer = make_indexer(session_factory, provider)
    indexer.index_file(grasp.id)
    first = len(provider.embedded)
    ids = {c.id for c in chunks(db, grasp)}

    again = indexer.index_file(grasp.id)
    assert (again.embedded, len(provider.embedded)) == (0, first)  # nothing changed
    assert {c.id for c in chunks(db, grasp)} == ids  # same derived ids

    page5 = db.scalar(select(Slide).where(Slide.file_id == grasp.id, Slide.page_no == 5))
    page5.content_json = {**page5.content_json, "content_markdown": "Coupling: fewer links."}
    page4 = db.scalar(select(Slide).where(Slide.file_id == grasp.id, Slide.page_no == 4))
    page4.notes = None
    db.commit()
    result = indexer.index_file(grasp.id)
    assert (result.embedded, result.removed) == (1, 1)
    assert provider.embedded[-1].endswith("Coupling: fewer links.")
    assert not [c for c in chunks(db, grasp) if c.source_type == "notes"]


def test_deleting_a_file_deletes_its_chunks(db, session_factory, subject, storage):
    grasp, _, states = build_subject(db, subject)
    indexer = make_indexer(session_factory, FakeQAProvider())
    indexer.index_file(grasp.id)
    indexer.index_file(states.id)
    delete_file(db, storage, grasp)
    assert {c.file_id for c in chunks(db)} == {states.id}


def test_worker_indexes_decks_and_transcripts_after_processing(db, session_factory, storage,
                                                               subject):  # fmt: skip
    provider = FakeProvider()
    llm = make_llm(session_factory, provider)
    transcripts = TranscriptProcessor(
        session_factory, storage, llm,
        TranscriptSettings("gpt-6.1-sol", "low", "text-embedding-3-large", 256, concurrency=2),
    )  # fmt: skip
    queue = ParseQueue(
        Parser(session_factory, storage, FakeVision), transcripts, executor=InlineExecutor(),
        indexer=make_indexer(session_factory, provider),
    )  # fmt: skip
    deck = upload(db, storage, subject, "Week 4 Lecture 7 GRASP.pdf", make_deck(deck_pages()))
    vtt = upload(db, storage, subject, "Week 4 Lecture 7.vtt", lecture_vtt(ORDER))
    queue.enqueue(db, [vtt])  # parses the deck first, then cleans and aligns
    db.refresh(deck)
    db.refresh(vtt)
    assert {c.source_type for c in chunks(db, deck)} == {"slide"}
    assert len(chunks(db, vtt)) == len(ORDER)
    assert "indexed 5 chunks" in deck.status_detail  # 6 pages, one build step collapsed
    assert f"indexed {len(ORDER)} chunks" in vtt.status_detail
