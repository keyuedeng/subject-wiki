"""Tutorials in search and Ask: chunks (setup/admin left out, solutions included),
citations ("Workshop 4 · Q3"), equal ranking with lectures, "introduced in tutorial" in
where-answers, and the source panel. Model and embeddings are faked."""

import pytest
from sqlalchemy import select

from app.classify.types import FileType
from app.db.models import Chunk, File, TutorialSection, tutorial_section_id
from app.main import app
from app.qa.where import detect_where
from app.services.ask import get_ask_service
from app.tutorials.names import sheet_names
from tests.search_fixtures import FakeQAProvider, _file, build_subject, make_indexer
from tests.test_ask import service, sources_in


def add_sheet(db, subject, filename, week, sections, ftype=FileType.TUTORIAL):
    """`sections`: (label, title, type, text, concepts, number)."""
    f = _file(db, subject, filename, ftype, week, None)
    rows = []
    for idx, (label, title, stype, text, concepts, number) in enumerate(sections):
        s = TutorialSection(
            id=tutorial_section_id(subject.id, f.sha256, idx), file_id=f.id, idx=idx,
            number=number, is_exercise=number is not None, label=label, title=title,
            text_md=text, words=len(text.split()), section_type=stype, concepts=concepts,
            coverage=("new_concept" if any(c["status"] != "covered" for c in concepts)
                      else "application") if concepts else None,
        )  # fmt: skip
        db.add(s)
        rows.append(s)
    db.flush()
    return f, rows


def concept(name, covered=True, summary=""):
    return {"name": name, "status": "covered" if covered else "not_covered",
            "evidence": [], "earliest_week": None, "summary": summary}  # fmt: skip


@pytest.fixture
def indexed(db, session_factory, subject):
    lectures = build_subject(db, subject)
    tute, rows = add_sheet(db, subject, "Workshop_04.pdf", 5, [
        ("Workshop 4", "Workshop 4: Fundamental Design Patterns", "setup_admin",
         "Completion: attend and participate in the workshop with your team.", [], None),
        ("Q1", "Exercise One - Creator", "exercise",
         "Decide which class should create Bid objects in the online auction marketplace, "
         "using the Creator pattern.", [concept("Creator")], 1),
        ("Q2", "Exercise Two - Gossip", "exercise",
         "Use a gossip protocol to spread listing updates between auction servers.",
         [concept("gossip protocol", False,
                  "Servers spread an update by telling random peers each round.")], 2),
    ])  # fmt: skip
    sols, answers = add_sheet(db, subject, "Partial_Solution_04.pdf", 5, [
        ("Q1", "Exercise One - Creator", None,
         "The Listing creates Bids: it aggregates them and has their initializing data.",
         [], 1),
    ], FileType.SOLUTIONS)  # fmt: skip
    rows[1].solution_section_id, rows[1].solution_source = answers[0].id, "official"
    db.commit()
    indexer = make_indexer(session_factory, FakeQAProvider())
    for f in [*lectures, tute, sols]:
        indexer.index_file(f.id)
    return tute, rows


def test_sheet_names():
    def f(name, week, title=None):
        return File(id=name, type="tutorial", filename=name, title=title, week=week,
                    lecture=None)  # fmt: skip

    files = [f("Workshop_04.pdf", 5), f("Tutorial '26W2 STUDENTS - Tabletop.docx", 2),
             f("No Kali W3 - Hashcat.docx", 3), f("companion.pdf", 4, "Low-Level Fundamentals"),
             f("handout.pdf", 4, "Low-Level Security")]  # fmt: skip
    assert sheet_names(files, "INFO30006") == {
        "Workshop_04.pdf": "Workshop 4",
        "Tutorial '26W2 STUDENTS - Tabletop.docx": "Week 2 tutorial",  # '26 is the year
        "No Kali W3 - Hashcat.docx": "Week 3 tutorial",
        "companion.pdf": "Low-Level Fundamentals",  # two week-4 sheets: their own titles
        "handout.pdf": "Low-Level Security",
    }


def test_tutorial_chunks_leave_out_setup_and_include_solutions(db, indexed):
    tute, rows = indexed
    chunks = {c.meta["label"]: c for c in db.scalars(
        select(Chunk).where(Chunk.file_id == tute.id))}  # fmt: skip
    assert set(chunks) == {"Q1", "Q2"}  # the setup section isn't searchable
    q1 = chunks["Q1"]
    assert "Solution (official):\nThe Listing creates Bids" in q1.body
    assert q1.meta["has_solution"] and q1.source_type == "tutorial"
    assert chunks["Q2"].meta["new_concepts"][0]["name"] == "gossip protocol"
    # The solutions sheet has no chunks of its own: its text lives with the exercise.
    assert not db.scalars(select(Chunk).where(Chunk.file_id != tute.id,
                                              Chunk.source_type == "tutorial")).all()  # fmt: skip


def test_answer_cites_tutorials_like_lectures(db, session_factory, subject, indexed):
    tute, rows = indexed

    def answer(text):
        found = sources_in(text)
        h = next(h for h, head in found.items() if head.startswith('Tutorial sheet "Workshop 4"'))
        return {"status": "answered", "missing": None, "cited": [h],
                "answer_markdown": f"The Listing creates Bids [{h}]."}  # fmt: skip

    provider = FakeQAProvider(answer=answer)
    r = service(session_factory, provider).ask(
        db, subject, "Which class should create Bid objects in the auction?"
    )
    ref = r.cited[0]
    assert ref.label == "Workshop 4 · Q1"
    assert ref.href == f"/files/{tute.id}/tutorial#sec-1"
    request = provider.structured_calls[0]
    assert ('Tutorial sheet "Workshop 4", filed in Week 5, section Q1 — Exercise One - Creator '
            "[includes the official solution]") in request  # fmt: skip
    # Ranked on equal terms: the tutorial, which matches best, comes first.
    assert r.sources[0].ref.source_type == "tutorial"


@pytest.mark.parametrize(("question", "topic"), [
    ("Which tutorial covers gossip protocols?", "gossip protocols"),
    ("In which workshop was the Creator pattern taught?", "Creator pattern"),
])  # fmt: skip
def test_where_detects_tutorial_phrasing(question, topic):
    assert detect_where(question) == topic


def test_topic_only_in_tutorials_is_introduced_in_tutorial(db, session_factory, subject, indexed):
    def where(text):
        found = sources_in(text)
        return {"items": [{"id": h, "level": "main"} for h, head in found.items()
                          if "Gossip" in head]}  # fmt: skip

    r = service(session_factory, FakeQAProvider(where=where)).ask(
        db, subject, "Where is the gossip protocol taught?"
    )
    assert r.found and r.introduced_in_tutorial
    assert r.tutorial_summary == "Servers spread an update by telling random peers each round."
    assert [(w.week, w.items[0].short_label) for w in r.weeks] == [(5, "Workshop 4 · Q2")]

    def lectures_too(text):
        return {"items": [{"id": h, "level": "main"} for h, head in sources_in(text).items()
                          if "Creator" in head]}  # fmt: skip

    both = service(session_factory, FakeQAProvider(where=lectures_too)).ask(
        db, subject, "Where is Creator taught?"
    )
    assert both.found and not both.introduced_in_tutorial


def test_tutorial_source_panel(db, session_factory, subject, indexed, client):
    tute, rows = indexed
    chunk = db.scalar(select(Chunk).where(Chunk.file_id == tute.id, Chunk.position == 1))
    html = client.get(f"/subjects/{subject.id}/ask/source/{chunk.id}").text
    assert "Workshop 4 · filed in Week 5" in html and "Q1 · Exercise One - Creator" in html
    assert "Show solution" in html and "The Listing creates Bids" in html
    assert f'href="/files/{tute.id}/tutorial#sec-1"' in html
    gossip = db.scalar(select(Chunk).where(Chunk.file_id == tute.id, Chunk.position == 2))
    assert (
        "Introduced in tutorial"
        in client.get(f"/subjects/{subject.id}/ask/source/{gossip.id}").text
    )
    app.dependency_overrides.pop(get_ask_service, None)
