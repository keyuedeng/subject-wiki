"""Transcripts end to end: pairing, cleaning, flags, alignment, caching, stable slide ids.
The model provider is faked (tests/transcript_fixtures.py); no network."""

from concurrent.futures import Future

import pytest
from sqlalchemy import select

from app.db.models import File, Slide, TranscriptDeck, TranscriptSegment, slide_id
from app.jobs.worker import ParseQueue, get_queue
from app.llm.cache import LlmCache
from app.llm.client import LlmClient
from app.main import app
from app.parse.pipeline import Parser
from app.services.uploads import ingest_upload
from app.transcripts.pipeline import TranscriptProcessor, TranscriptSettings
from tests.slide_fixtures import make_deck
from tests.test_pipeline import FakeVision
from tests.transcript_fixtures import TOPICS, FakeProvider, deck_pages, lecture_vtt

# Lecture order: topics 0,1,2, a tangent, 3, back to 1, then 4.
ORDER = [0, 0, 1, 1, 2, 2, None, 3, 3, 1, 4, 4]


class InlineExecutor:
    def submit(self, fn, *args):
        f = Future()
        f.set_result(fn(*args))
        return f

    def shutdown(self, **_):
        pass


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def queue(session_factory, storage, provider):
    parser = Parser(session_factory, storage, FakeVision)
    transcripts = TranscriptProcessor(
        session_factory, storage, LlmClient(lambda: provider, LlmCache(session_factory)),
        TranscriptSettings("gpt-6.1-sol", "low", "text-embedding-3-large", 256, concurrency=2),
    )  # fmt: skip
    return ParseQueue(parser, transcripts, executor=InlineExecutor())


def upload(db, storage, subject, name, data):
    return ingest_upload(db, storage, subject, name, None, data, max_bytes=50_000_000).file


@pytest.fixture
def lecture(db, storage, subject):
    deck = upload(db, storage, subject, "Week 4 Lecture 7 GRASP.pdf", make_deck(deck_pages()))
    vtt = upload(db, storage, subject, "Week 4 Lecture 7.vtt", lecture_vtt(ORDER))
    assert (deck.type, vtt.type, deck.lecture, vtt.lecture) == ("slides", "transcript", 7, 7)
    return deck, vtt


def segments(db, file) -> list[TranscriptSegment]:
    db.expire_all()
    q = select(TranscriptSegment).where(TranscriptSegment.file_id == file.id)
    return list(db.scalars(q.order_by(TranscriptSegment.idx)))


def topic_of(db, seg) -> int | None:
    if seg.slide_id is None:
        return None
    title = db.get(Slide, seg.slide_id).title
    return next(i for i, (t, _) in enumerate(TOPICS) if t == title)


def test_process_pairs_parses_deck_cleans_flags_and_aligns(db, queue, lecture, provider):
    deck, vtt = lecture
    queue.enqueue(db, [vtt])  # the deck isn't parsed yet: it's parsed first
    db.refresh(vtt)
    db.refresh(deck)
    assert deck.status == "parsed"
    assert vtt.status == "parsed", vtt.status_detail
    segs = segments(db, vtt)
    assert len(segs) == len(ORDER)
    assert [topic_of(db, s) for s in segs] == ORDER  # incl. the revisit and the tangent

    # Clean text: fillers gone, raw kept alongside.
    assert "Um " in segs[0].raw_text and "Um " not in segs[0].clean_text
    # Flags: exam hint on window 2, student question on window 4.
    assert [f["type"] for f in segs[2].flags] == ["exam_hint"]
    assert "student_question" in [f["type"] for f in segs[4].flags]
    # Pairing was automatic (same week and lecture number).
    pair = db.scalar(select(TranscriptDeck))
    assert (pair.slides_file_id, pair.source) == (deck.id, "auto")
    # Build-step pages are never alignment targets.
    kept = {s.id for s in db.scalars(select(Slide).where(Slide.duplicate_of.is_(None)))}
    assert {s.slide_id for s in segs if s.slide_id} <= kept
    assert vtt.has_timestamps
    assert "12 windows" in vtt.status_detail and "1 unaligned" in vtt.status_detail


def test_rerun_makes_no_calls(db, queue, lecture, provider):
    _, vtt = lecture
    queue.enqueue(db, [vtt])
    calls, embedded = len(provider.structured_calls), len(provider.embedded)
    db.refresh(vtt)
    queue.enqueue(db, [vtt])
    assert len(provider.structured_calls) == calls and len(provider.embedded) == embedded
    db.refresh(vtt)
    assert "this run $0.00" in vtt.status_detail
    assert all(s.cached for s in segments(db, vtt))


def test_deck_reparse_keeps_slide_ids_and_realigns_for_free(db, queue, lecture, provider):
    deck, vtt = lecture
    queue.enqueue(db, [vtt])
    before = [s.slide_id for s in segments(db, vtt)]
    calls = len(provider.structured_calls)
    db.refresh(deck)

    queue.enqueue(db, [deck])  # re-parse the deck: paired transcript is re-aligned

    after = [s.slide_id for s in segments(db, vtt)]
    assert after == before  # same derived ids, so links survive
    assert len(provider.structured_calls) == calls  # cleaning came from the cache
    ids = {s.id for s in db.scalars(select(Slide).where(Slide.file_id == deck.id))}
    assert ids == {slide_id(deck.subject_id, deck.sha256, n) for n in range(1, 7)}


def test_unpaired_transcript_fails_with_a_clear_message(db, storage, subject, queue):
    vtt = upload(db, storage, subject, "SWEN30006 transcript.vtt", lecture_vtt([0, 1]))
    queue.enqueue(db, [vtt])
    db.refresh(vtt)
    assert vtt.status == "failed" and "Pair this transcript" in vtt.status_detail


def test_manual_pairing_overrides_and_survives_auto_sync(db, storage, subject, queue, lecture):
    from app.transcripts.pairing import decks_for, reset_to_auto, set_manual, sync_auto_pairs

    deck, vtt = lecture
    other = upload(db, storage, subject, "Week 4 Lecture 8.pdf", make_deck(deck_pages()[:2]))
    set_manual(db, vtt.id, [other.id, deck.id])
    sync_auto_pairs(db, subject.id)
    assert [(f.id, src) for f, src in decks_for(db, vtt.id)] == [
        (other.id, "manual"), (deck.id, "manual"),
    ]  # fmt: skip
    reset_to_auto(db, vtt)
    assert [f.id for f, _ in decks_for(db, vtt.id)] == [deck.id]


def test_transcript_without_week_pairs_by_lecture_number(db, storage, subject):
    from app.transcripts.pairing import decks_for, sync_auto_pairs

    deck = upload(db, storage, subject, "Lecture 4 - GRASP.pdf", make_deck(deck_pages()))
    vtt = upload(db, storage, subject, "Lecture 4 captions.vtt", lecture_vtt([0]))
    sync_auto_pairs(db, subject.id)
    assert [f.id for f, _ in decks_for(db, vtt.id)] == [deck.id]


# Web


@pytest.fixture
def web(client, queue):
    app.dependency_overrides[get_queue] = lambda: queue
    return client


def test_viewer_transcript_view_and_board(web, db, queue, lecture, subject):
    deck, vtt = lecture
    web.post(f"/files/{vtt.id}/parse?view=row")

    # Slide viewer: "What was said" under slide 1 (Creator).
    page = web.get(f"/files/{deck.id}/slides?page=1").text
    assert "What was said" in page and "creator assigns creation" in page
    assert "0:00–" in page
    # A build-step page shows what was said on its kept page.
    build = web.get(f"/files/{deck.id}/slides?page=4").text
    assert "is a build step of page 5" in build and "cohesion focused" in build

    # Transcript view: runs labelled by slide, the tangent marked, flags shown.
    tv = web.get(f"/files/{vtt.id}/transcript").text
    assert "Slide 1 · Creator Pattern" in tv and "Not matched to a slide" in tv
    assert "Exam hint" in tv and "matched by lecture number" in tv
    exam_only = web.get(f"/files/{vtt.id}/transcript?flag=exam_hint").text
    assert exam_only.count('class="seg"') == 1

    # Board: the transcript sits beside its deck, linked to the transcript view.
    board = web.get(f"/subjects/{subject.id}/view/board").text
    assert "matched to slides" in board and f'href="/files/{vtt.id}/transcript"' in board


def test_slide_link_resolves_build_steps(web, db, queue, lecture):
    deck, _ = lecture
    queue.enqueue(db, [deck])
    build = db.scalar(select(Slide).where(Slide.file_id == deck.id, Slide.page_no == 4))
    r = web.get(f"/slides/{build.id}", follow_redirects=False)
    assert r.headers["location"] == f"/files/{deck.id}/slides?page=5"


def test_pairing_controls(web, db, storage, subject, lecture):
    deck, vtt = lecture
    other = upload(db, storage, subject, "Week 5 Lecture 9.pdf", make_deck(deck_pages()[:1]))
    web.post(f"/files/{vtt.id}/pairing/add", data={"slides_id": str(other.id)})
    tv = web.get(f"/files/{vtt.id}/transcript").text
    assert tv.count("set by you") == 2 and "Reset to automatic" in tv
    web.post(f"/files/{vtt.id}/pairing/remove", data={"slides_id": str(deck.id)})
    api = web.get(f"/api/files/{vtt.id}/pairing").json()
    assert api == {"slides_file_ids": [str(other.id)], "source": "manual"}
    web.post(f"/files/{vtt.id}/pairing/reset")
    assert web.get(f"/api/files/{vtt.id}/pairing").json()["slides_file_ids"] == [str(deck.id)]
    bad = web.post(f"/files/{vtt.id}/pairing/add", data={"slides_id": str(vtt.id)})
    assert bad.status_code == 422


def test_segments_api(web, db, queue, lecture):
    _, vtt = lecture
    queue.enqueue(db, [vtt])
    data = web.get(f"/api/files/{vtt.id}/segments").json()
    assert len(data) == len(ORDER) and data[2]["flags"][0]["type"] == "exam_hint"


def test_embeddings_are_cached_per_text(session_factory, provider):
    llm = LlmClient(lambda: provider, LlmCache(session_factory))
    v1, _, calls1 = llm.embed(model="m", dimensions=8, texts=["alpha beta", "gamma", "alpha beta"])
    assert calls1 == 2 and v1[0] == v1[2]  # duplicate text embedded once
    _, cost, calls2 = llm.embed(model="m", dimensions=8, texts=["gamma", "delta"])
    assert calls2 == 1  # only "delta" was new
    assert File  # (imported for typing in other tests)
