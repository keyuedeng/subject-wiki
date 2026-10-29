"""Cited Q&A and "where is X taught": citation checks, not-covered behaviour, coverage
lists by week, labels, follow-ups and the JSON API. The model and embeddings are faked.
The Ask page and chats are in test_chat.py."""

import re
import uuid

import pytest
from pydantic import ValidationError

from app.main import app
from app.qa.answer import Turn, answer_schema, check, handles, request_text
from app.qa.labels import SourceRef
from app.qa.where import detect_where, group_by_week, week_ranges
from app.search.backend import Filters
from app.services.ask import AskService, AskSettings, get_ask_service
from tests.search_fixtures import (
    FakeQAProvider,
    MemoryBackend,
    StreamingQAProvider,
    build_subject,
    make_indexer,
    make_llm,
)


@pytest.fixture
def indexed(db, session_factory, subject):
    files = build_subject(db, subject)
    indexer = make_indexer(session_factory, FakeQAProvider())
    for f in files:
        indexer.index_file(f.id)
    return files


def service(session_factory, provider) -> AskService:
    settings = AskSettings("gpt-6.1-sol", "low", "text-embedding-3-large", 256)
    return AskService(make_llm(session_factory, provider), settings, MemoryBackend())


def sources_in(text: str) -> dict[str, str]:
    """Handle -> its header line, from a request the model received."""
    return dict(re.findall(r"^\[(S\d+)\] (.*)$", text, re.MULTILINE))


# Citation checks


def test_check_keeps_valid_citations_and_removes_unknown_ones():
    ids = handles(3)
    out = check({"status": "answered", "missing": None, "cited": ["S1"],
                 "answer_markdown": "Creator aggregates [S2, S1]. Expert [S9]. Both [S3; S2]."},
                ids)  # fmt: skip
    assert out.answer_markdown == "Creator aggregates [S2][S1]. Expert. Both [S3][S2]."
    assert out.cited == ["S2", "S1", "S3"]  # inline use decides, in order of first use
    assert out.invalid == ["S9"]
    assert out.warnings == ["Removed 1 citation(s) that pointed to no retrieved source."]


def test_check_flags_an_answer_without_citations():
    out = check({"status": "answered", "answer_markdown": "Trust me.", "cited": [],
                 "missing": None}, handles(2))  # fmt: skip
    assert out.cited == [] and "unverified" in out.warnings[0]
    covered = check({"status": "not_covered", "answer_markdown": "Not covered.", "cited": [],
                     "missing": "x"}, handles(2))  # fmt: skip
    assert covered.warnings == []


def test_schema_only_accepts_retrieved_handles():
    schema = answer_schema(handles(2))
    ok = {
        "status": "answered",
        "answer_markdown": "[S2]",
        "cited": ["S2"],
        "missing": None,
        "topic": "Creator",
        "follow_ups": ["Why?", "How?"],
    }
    assert schema.model_validate(ok).cited == ["S2"]
    with pytest.raises(ValidationError):
        schema.model_validate({**ok, "cited": ["S3"]})


# Answers


def test_answer_cites_real_retrieved_chunks(db, session_factory, subject, indexed):
    def answer(text):
        found = sources_in(text)
        creator = next(h for h, head in found.items() if head.startswith("Slide 1 "))
        said = next(h for h, head in found.items() if head.startswith("Lecture transcript"))
        return {"status": "answered", "missing": None, "cited": [creator, said, "S1"],
                "answer_markdown": f"The aggregating class creates the parts [{creator}], "
                                   f"and it's examinable [{said}] [S99]."}  # fmt: skip

    provider = FakeQAProvider(answer=answer)
    r = service(session_factory, provider).ask(
        db, subject, "Which class should create instances of the aggregated parts?"
    )
    assert r.kind == "answer" and r.status == "answered"
    assert [ref.label for ref in r.cited] == ["Week 4 · slide 1", "Week 4 lecture · 0:45"]
    assert all(ref in r.handles.values() for ref in r.cited)
    assert len(r.sources) <= 8 and r.warnings  # the bogus [S99] was removed and reported
    request = provider.structured_calls[0]
    assert "likely to be examined" in request  # the exam-hint flag reaches the model
    assert "Welcome back" not in request  # admin talk is never a source

    calls = len(provider.structured_calls)
    again = service(session_factory, provider).ask(
        db, subject, "Which class should create instances of the aggregated parts?"
    )
    assert again.cached and len(provider.structured_calls) == calls


def test_unrelated_question_is_not_covered_without_a_model_call(db, session_factory, subject,
                                                                indexed):  # fmt: skip
    provider = FakeQAProvider()
    r = service(session_factory, provider).ask(db, subject, "How long should sourdough proof?")
    assert (r.status, r.model_called, provider.structured_calls) == ("not_covered", False, [])


def test_model_saying_not_covered_is_passed_on(db, session_factory, subject, indexed):
    provider = FakeQAProvider(
        answer=lambda text: {
            "status": "not_covered",
            "cited": [],
            "missing": "how the Adapter pattern works",
            "answer_markdown": "The uploaded material doesn't explain the Adapter pattern.",
        }
    )
    r = service(session_factory, provider).ask(
        db, subject, "Which class should create instances of an adapter?"
    )
    assert (r.status, r.model_called, r.cited) == ("not_covered", True, [])
    assert r.missing == "how the Adapter pattern works" and r.sources and not r.warnings


def test_filters_that_match_nothing_say_so(db, session_factory, subject, indexed):
    r = service(session_factory, FakeQAProvider()).ask(
        db, subject, "What is a guard?", Filters(weeks=(6,), types=("notes",))
    )
    assert r.status == "not_covered" and "selected weeks and sources" in r.answer_markdown


# "Where is X taught?"


@pytest.mark.parametrize(("question", "topic"), [
    ("Where is the Creator pattern taught?", "Creator pattern"),
    ("where was information expert covered in the lectures", "information expert"),
    ("Which week covers state machines?", "state machines"),
    ("What lecture is the Observer pattern in?", "Observer pattern"),
    ("When did we learn about low coupling?", "low coupling"),
    ("Where can I find GRASP in the slides?", "GRASP"),
    ("In which week was cohesion introduced?", "cohesion"),
    ("What is the Creator pattern?", None),
    ("Where is the state stored in the State pattern?", None),
    ("Why is high cohesion good?", None),
])  # fmt: skip
def test_detect_where(question, topic):
    assert detect_where(question) == topic


def ref(source_type, week, position, file_id, **meta) -> SourceRef:
    meta.setdefault("page_no", position)
    meta.setdefault("slide_id", str(uuid.uuid4()))
    return SourceRef(uuid.uuid4(), source_type, file_id, "Deck", week, week, position,
                     f"Title {position}", "body", meta)  # fmt: skip


def test_group_by_week_orders_and_collapses_neighbours():
    deck, vtt, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    found = [
        (ref("transcript", 4, 8, vtt, idx=8, start_ms=360_000, end_ms=405_000), "main"),
        (ref("slide", 7, 2, other), "mentioned"),
        (ref("slide", 4, 14, deck), "main"),
        (ref("slide", 4, 12, deck), "main"),  # 12 and 14: a collapsed build step between
        (ref("transcript", 4, 9, vtt, idx=9, start_ms=405_000, end_ms=450_000), "main"),
        (ref("slide", 4, 20, deck), "main"),  # too far from 14: its own item
        (ref("slide", 4, 21, deck), "mentioned"),  # different level: its own item
    ]
    weeks = group_by_week(found)
    assert [w.week for w in weeks] == [4, 7]
    assert [(i.level, i.label) for i in weeks[0].items] == [
        ("main", "Week 4 · slides 12–14"),
        ("main", "Week 4 · slide 20"),
        ("mentioned", "Week 4 · slide 21"),
        ("main", "Week 4 lecture · 6:00–7:30"),
    ]
    assert weeks[1].items[0].label == "Week 7 · slide 2"


def test_where_returns_coverage_by_week(db, session_factory, subject, indexed):
    def where(text):
        found = sources_in(text)
        items = []
        for h, head in found.items():
            if "GRASP: Creator" in head or "while the slide 'GRASP: Creator'" in head:
                items.append({"id": h, "level": "main"})
            elif "Controller" in head:
                items.append({"id": h, "level": "mentioned"})
        return {"items": items}

    provider = FakeQAProvider(where=where)
    r = service(session_factory, provider).ask(db, subject, "Where is Creator taught?")
    assert r.kind == "where" and r.topic == "Creator" and r.found
    assert "Topic: Creator" in provider.structured_calls[0]
    week4 = r.weeks[0]
    assert week4.week == 4
    labels = [(i.level, i.label) for i in week4.items]
    assert ("main", "Week 4 · slide 1") in labels
    assert ("main", "Week 4 lecture · 0:45–2:15") in labels  # two windows, one moment
    assert all(lvl == "main" or label.endswith("slide 4") for lvl, label in labels
               if "lecturer notes" not in label)  # fmt: skip


def test_where_not_found_lists_missing_weeks(db, session_factory, subject, indexed):
    provider = FakeQAProvider(where=lambda text: {"items": []})
    r = service(session_factory, provider).ask(db, subject, "Which week covers the creator?")
    assert r.kind == "where" and not r.found
    assert r.missing_note == (
        "Weeks 1–3, 5 and 7–12 have no slides or transcripts uploaded yet. "
        "Lecture transcripts are missing for week 6."
    )
    forced = service(session_factory, provider).ask(db, subject, "sourdough", mode="where")
    assert forced.kind == "where" and not forced.found and not forced.model_called


def test_week_ranges():
    assert week_ranges([9, 1, 2, 3, 5, 8]) == "1–3, 5, 8–9"
    assert week_ranges([4]) == "4"


# Labels and the Ask page


def test_labels_and_links():
    deck = uuid.uuid4()
    slide = ref("slide", 4, 14, deck)
    assert (slide.label, slide.href) == ("Week 4 · slide 14", f"/slides/{slide.meta['slide_id']}")
    notes = ref("notes", 4, 3, deck)
    assert notes.label == "Week 4 lecturer notes · slide 3" and notes.href.endswith("#notes")
    said = ref("transcript", 4, 31, deck, idx=31, start_ms=1_420_000)
    assert said.label == "Week 4 lecture · 23:40"
    assert said.href == f"/files/{deck}/transcript#seg-31"
    two_decks = SourceRef(uuid.uuid4(), "slide", deck, "Deck", 4, 5, 2, "T", "b",
                          {"page_no": 2, "slide_id": "x"}, multi_lecture_week=True)  # fmt: skip
    assert two_decks.label == "Week 4 L5 · slide 2"


# Follow-ups


def test_follow_up_uses_earlier_turns_for_retrieval_and_the_answer(db, session_factory, subject,
                                                                   indexed):  # fmt: skip
    provider = FakeQAProvider()
    history = [
        Turn(
            "Which class should create the aggregated parts?",
            "The container that aggregates them creates them [S1].",
            "Creator pattern",
        )
    ]
    r = service(session_factory, provider).ask(
        db, subject, "And is that on the exam?", history=history
    )
    request = provider.structured_calls[-1]
    assert request.startswith("Earlier in this conversation")
    assert "Student: Which class should create the aggregated parts?" in request
    assert "creates them [S1]" not in request and "creates them." in request  # markers removed
    # Retrieval also searched with the previous question, so Creator sources were found
    # though the follow-up names nothing.
    assert "Which class should create the aggregated parts? And is that on the exam?" in (
        provider.embedded
    )
    assert any("GRASP: Creator" in src.ref.title for src in r.sources)


def test_where_follow_up_resolves_this_to_the_previous_topic(db, session_factory, subject,
                                                            indexed):  # fmt: skip
    provider = FakeQAProvider()
    history = [Turn("Who should create an object?", "Creator says … [S1]", "Creator")]
    r = service(session_factory, provider).ask(
        db, subject, "Where is this taught?", history=history
    )
    assert r.kind == "where" and r.topic == "Creator"


def test_request_text_without_history_starts_with_the_question():
    assert request_text("What is X?", []).startswith("Question: What is X?")


def test_events_report_sources_then_streamed_text(db, session_factory, subject, indexed):
    provider = StreamingQAProvider()
    events = []
    r = service(session_factory, provider).ask(
        db, subject, "Which class should create instances of the aggregated parts?",
        on_event=lambda kind, data: events.append((kind, data)),
    )  # fmt: skip
    kinds = [k for k, _ in events]
    assert kinds[0] == "sources" and set(kinds[1:]) == {"text"}
    assert "".join(d for k, d in events if k == "text").startswith('{"status":"answered"')
    assert r.answer_markdown == "See [S1]."
    events.clear()
    service(session_factory, provider).ask(
        db, subject, "Which class should create instances of the aggregated parts?",
        on_event=lambda kind, data: events.append((kind, data)),
    )  # fmt: skip
    assert [k for k, _ in events] == ["sources"]  # cached: nothing to stream


def test_api_ask_returns_cited_chunk_ids(db, session_factory, subject, indexed, client):
    svc = service(session_factory, FakeQAProvider())
    app.dependency_overrides[get_ask_service] = lambda: svc
    question = "Which class should create instances of the aggregated parts?"
    body = client.post(f"/api/subjects/{subject.id}/ask", json={"question": question}).json()
    assert body["kind"] == "answer" and body["status"] == "answered"
    retrieved = {s["chunk_id"] for s in body["sources"]}
    assert body["cited_chunk_ids"] and set(body["cited_chunk_ids"]) <= retrieved
    hits = client.post(f"/api/subjects/{subject.id}/search",
                       json={"query": "guard", "types": ["slide"], "k": 3}).json()  # fmt: skip
    assert hits and all(h["source_type"] == "slide" for h in hits)
