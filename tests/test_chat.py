"""The Ask page: chats, streamed answers, saved conversations, follow-ups and the source
preview panel. The model streams from a fake; search runs in memory."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models import Chat, ChatMessage, Chunk, File, Subject, chunk_id, slide_id
from app.main import app
from app.services import chat as chats
from app.services.ask import AskService, AskSettings, get_ask_service
from tests.search_fixtures import (
    FakeQAProvider,
    MemoryBackend,
    StreamingQAProvider,
    build_subject,
    make_indexer,
    make_llm,
)

QUESTION = "Which class should create instances of the aggregated parts?"


@pytest.fixture
def indexed(db, session_factory, subject):
    files = build_subject(db, subject)
    indexer = make_indexer(session_factory, FakeQAProvider())
    for f in files:
        indexer.index_file(f.id)
    return files


@pytest.fixture
def use(session_factory):
    """Install a provider for the Ask routes."""

    def install(provider):
        settings = AskSettings("gpt-6.1-sol", "low", "text-embedding-3-large", 256)
        svc = AskService(make_llm(session_factory, provider), settings, MemoryBackend())
        app.dependency_overrides[get_ask_service] = lambda: svc
        return provider

    return install


def creator_answer(text):
    md = "The **aggregating** class creates the parts [{}]."
    handle = next(line[1:3] for line in text.splitlines() if "GRASP: Creator" in line
                  and line.startswith("[S"))  # fmt: skip
    return {"status": "answered", "missing": None, "cited": [handle], "topic": "Creator",
            "follow_ups": ["How is Creator different from Expert?", "Walk me through Monopoly"],
            "answer_markdown": md.format(handle)}  # fmt: skip


def ask(client, subject, q=QUESTION, **form):
    r = client.post(f"/subjects/{subject.id}/ask/stream", data={"q": q, **form})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def slide_chunk(subject, deck, page) -> str:
    return str(chunk_id(subject.id, f"slide:{slide_id(subject.id, deck.sha256, page)}"))


# Streaming and saving


def test_first_question_creates_a_chat_and_streams_the_answer(db, subject, indexed, client, use):
    provider = use(StreamingQAProvider(answer=creator_answer))
    events = ask(client, subject)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done"
    assert "status" in kinds and "delta" in kinds and provider.streamed
    start, done = events[0], events[-1]
    assert QUESTION[:40] in start["sidebar"]  # the new chat is listed straight away
    delta = next(e for e in events if e["type"] == "delta")
    assert delta["html"].startswith("<p>Th") and "[S" not in delta["html"]  # a prefix, no markers

    html = done["html"]
    assert "Answered from Week 4 · 1 source" in html
    assert f'data-preview="/subjects/{subject.id}/ask/source/' in html  # chips open the panel
    assert 'class="cite cite-slide"' in html and "Week 4 · slide 1" in html
    assert "How is Creator different from Expert?" in html  # follow-ups
    assert 'data-copy="The **aggregating** class creates the parts [Week 4 · slide 1]."' in html
    assert 'data-ask="Where is Creator taught?"' in html and 'data-mode="where"' in html

    chat = db.scalar(select(Chat))
    assert start["chat_id"] == str(chat.id) and start["url"].endswith(str(chat.id))
    assert chat.title == "Which class should create instances of the aggregated parts?"
    db.refresh(chat)
    question, answer = chat.messages
    assert (question.role, question.kind, answer.role, answer.kind) == (
        "user", "question", "assistant", "answer")  # fmt: skip
    assert answer.payload["topic"] == "Creator" and answer.payload["cited"]


def test_follow_up_keeps_the_conversation(db, subject, indexed, client, use):
    provider = use(FakeQAProvider(answer=creator_answer))
    chat_id = ask(client, subject)[0]["chat_id"]
    events = ask(client, subject, "Is that on the exam?", chat_id=chat_id)
    assert events[0]["chat_id"] == chat_id and "sidebar" not in events[0]
    assert provider.structured_calls[-1].startswith("Earlier in this conversation")
    assert QUESTION in provider.structured_calls[-1]
    assert db.scalar(select(Chat).where(Chat.id == Chat.id)) is not None
    assert len(db.scalars(select(ChatMessage)).all()) == 4


def test_saved_chat_renders_from_its_snapshot(db, subject, indexed, client, use):
    use(FakeQAProvider(answer=creator_answer))
    chat_id = ask(client, subject)[0]["chat_id"]
    page = client.get(f"/subjects/{subject.id}/ask/{chat_id}").text
    assert f'<div class="bubble">{QUESTION}</div>' in page
    assert "Week 4 · slide 1" in page and 'aria-current="page"' in page and "Today" in page
    assert "Searching 2 lectures · " in page

    # Deleting the deck removes its chunks, but the saved answer still shows its sources.
    deck = db.scalar(select(File).where(File.filename == "Lecture 4 GRASP.pdf"))
    db.delete(deck)
    db.commit()
    assert not db.scalars(select(Chunk).where(Chunk.file_id == deck.id)).all()
    again = client.get(f"/subjects/{subject.id}/ask/{chat_id}").text
    assert "Week 4 · slide 1" in again and "aggregating" in again


def test_where_answer_lists_coverage_by_week(db, subject, indexed, client, use):
    def where(text):
        items = [{"id": line[1:3], "level": "main"} for line in text.splitlines()
                 if line.startswith("[S") and "GRASP: Creator" in line]  # fmt: skip
        return {
            "summary": "Creator is taught in Week 4, on slide 1.",
            "items": items,
            "follow_ups": ["Explain Creator", "Where is Information Expert taught?"],
        }

    use(FakeQAProvider(where=where))
    events = ask(client, subject, "Creator", mode="where")
    assert any(e["type"] == "status" and "Finding where Creator is taught" in e["text"]
               for e in events)  # fmt: skip
    html = events[-1]["html"]
    assert "Creator is taught in Week 4, on slide 1." in html
    assert "<h2>Week 4</h2>" in html and "Main coverage" in html and ">Slide 1<" in html
    assert "Lecture transcripts are missing for week 6." in html and "Open the board" in html
    assert html.count("Explain Creator") == 2  # one button (label + data-ask), not repeated
    assert "Where is Information Expert taught?" in html


def test_not_covered_answer_is_a_card_linking_to_the_board(subject, indexed, client, use):
    use(FakeQAProvider(answer=lambda text: {
        "status": "not_covered", "cited": [], "missing": "Adapter",
        "answer_markdown": "None of the slides explain Adapter."}))  # fmt: skip
    html = ask(client, subject, "Which class should create an adapter?")[-1]["html"]
    assert "Not explained in your uploaded material" in html
    assert f'href="/subjects/{subject.id}">See what\'s missing on the board' in html
    assert "sources checked" in html


def test_failure_is_saved_and_left_out_of_the_history(db, subject, indexed, client, use):
    class Broken(FakeQAProvider):
        def structured(self, **kwargs):
            from app.llm.client import ProviderError

            raise ProviderError("Model response incomplete (max_output_tokens)")

    use(Broken())
    events = ask(client, subject)
    assert events[-1]["type"] == "error" and "max_output_tokens" in events[-1]["html"]
    chat = db.scalar(select(Chat))
    db.refresh(chat)
    assert [m.kind for m in chat.messages] == ["question", "error"]
    assert chats.history(chat) == []
    assert "Couldn't answer this" in client.get(f"/subjects/{subject.id}/ask/{chat.id}").text


def test_page_and_validation(db, session_factory, subject, indexed, client, use):
    use(FakeQAProvider())
    page = client.get(f"/subjects/{subject.id}/ask").text
    assert "Ask about COMP30027" in page and 'name="week" value="4"' in page
    assert 'aria-current="page">Ask</a>' in page  # top nav
    assert 'data-ask="Explain GRASP' in page  # suggestions from the indexed slides
    url = f"/subjects/{subject.id}/ask/stream"
    assert client.post(url, data={"q": "  "}).status_code == 422
    assert client.post(url, data={"q": "x", "mode": "essay"}).status_code == 422
    assert client.post(url, data={"q": "x", "chat_id": "nope"}).status_code == 404
    other = Subject(name="Other", weeks=12)
    db.add(other)
    db.commit()
    foreign = chats.new_chat(db, other, "Elsewhere")
    db.commit()
    assert client.post(url, data={"q": "x", "chat_id": str(foreign.id)}).status_code == 404
    assert client.get(f"/subjects/{subject.id}/ask/{foreign.id}").status_code == 404


def test_delete_chat(db, subject, indexed, client, use):
    use(FakeQAProvider())
    chat_id = ask(client, subject)[0]["chat_id"]
    r = client.delete(f"/subjects/{subject.id}/ask/{chat_id}?current=1")
    assert r.headers["HX-Redirect"] == f"/subjects/{subject.id}/ask"
    assert db.scalar(select(Chat)) is None and not db.scalars(select(ChatMessage)).all()


# Source preview panel


def test_slide_panel_shows_image_navigation_and_what_was_said(db, subject, indexed, client):
    grasp, vtt, _ = indexed
    html = client.get(f"/subjects/{subject.id}/ask/source/{slide_chunk(subject, grasp, 1)}").text
    assert "Week 4 · Lecture 4 · slide 1" in html and "GRASP: Creator" in html
    assert f'src="/files/{grasp.id}/pages/1.png"' in html
    assert f'href="/files/{grasp.id}/slides?page=1"' in html and "Open in slide viewer" in html
    # Lecture windows 1 and 2 were aligned to slide 1; the next kept slide is page 3
    # (page 2 is a build step of it).
    assert "the board creates the squares" in html and "0:45" in html
    assert "Worked example" in html and "Open full transcript" in html
    assert f"/ask/slide/{slide_id(subject.id, grasp.sha256, 3)}" in html


def test_transcript_and_notes_chips_highlight_their_part(db, subject, indexed, client):
    grasp, vtt, _ = indexed
    said = chunk_id(subject.id, f"transcript:{vtt.sha256}:2")
    html = client.get(f"/subjects/{subject.id}/ask/source/{said}").text
    assert 'class="said-row hl"' in html and f"/files/{vtt.id}/transcript#seg-2" in html
    notes = chunk_id(subject.id, f"notes:{slide_id(subject.id, grasp.sha256, 4)}")
    html = client.get(f"/subjects/{subject.id}/ask/source/{notes}").text
    assert 'class="panel-notes hl"' in html and "not a UI class" in html


def test_panel_for_a_missing_source(subject, client):
    html = client.get(f"/subjects/{subject.id}/ask/source/{slide_id(subject.id, 'x', 1)}").text
    assert "no longer in the index" in html


# Titles and the sidebar


def test_chat_title_is_the_first_question_cut_at_a_word():
    assert chats.chat_title("  Who should   create an object? ") == "Who should create an object?"
    long = "Why does Creator prefer the class that aggregates the object over one that uses it?"
    assert chats.chat_title(long) == "Why does Creator prefer the class that aggregates the…"


def test_chat_groups_by_day(db, subject):
    now = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
    for title, age in [("today", 1), ("yesterday", 24), ("last week", 24 * 5), ("old", 24 * 30)]:
        db.add(Chat(subject_id=subject.id, title=title, updated_at=now - timedelta(hours=age)))
    db.commit()
    groups = chats.chat_groups(db, subject, now)
    assert [(h, [c.title for c in cs]) for h, cs in groups] == [
        ("Today", ["today"]), ("Yesterday", ["yesterday"]),
        ("Previous 7 days", ["last week"]), ("Older", ["old"]),
    ]  # fmt: skip
