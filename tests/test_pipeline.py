"""End-to-end parsing with a fake vision model: no network, no API key."""

import threading

import pytest
from sqlalchemy import select

from app.db.models import File, LlmCall, Slide, Subject
from app.parse import vision as vision_module
from app.parse.pipeline import Parser
from app.parse.schema import SlideContent, Visual, VisualKind
from app.parse.vision import Usage, VisionResult
from app.services.uploads import ingest_upload
from tests.slide_fixtures import POLL, STATE_MACHINE_BUILD, make_deck


class FakeVision:
    provider = "fake"
    model = "gpt-6.1-sol"
    prompt = "fake prompt v1"

    def fingerprint(self, png_sha256, text, build_captions):
        return {
            "provider": self.provider,
            "model": self.model,
            "instructions": self.prompt,
            "input_text": text,
            "captions": build_captions,
            "image_sha256": png_sha256,
        }

    def __init__(self, fail_pages=(), refuse_pages=()):
        self.calls: list[dict] = []
        self.fail_pages, self.refuse_pages = set(fail_pages), set(refuse_pages)
        self._lock = threading.Lock()

    def describe(self, png, text, build_captions):
        with self._lock:
            self.calls.append({"text": text, "captions": build_captions})
            n = len(self.calls)
        page_marker = next((t for t in ("FAIL", "REFUSE") if t in text), None)
        usage = Usage(input_tokens=3800, cached_tokens=0, output_tokens=500, reasoning_tokens=0)
        if page_marker == "FAIL":
            raise RuntimeError("simulated API error")
        if page_marker == "REFUSE":
            return VisionResult(None, usage, self.model, refusal="not allowed")
        content = SlideContent(
            title=f"Vision title {n}",
            content_markdown=f"# Vision page {n}",
            visuals=[Visual(kind=VisualKind.UML_STATE_MACHINE, description="States...",
                            key_facts=["Red --button--> Green"])],
            equations=[],
            code_blocks=[],
        )  # fmt: skip
        return VisionResult(content, usage, self.model)


def text_page(i: int) -> dict:
    return {"title": f"Topic {i}", "lines": [f"• Point {i}.{j}" for j in range(3)]}


# 8 pages: 3 text, 3-step diagram build (1 vision call), a poll shown twice (1 call),
# and a standalone diagram (1 call) -> 3 vision calls.
DECK = [
    text_page(0),
    *STATE_MACHINE_BUILD,
    POLL,
    {"title": "Big diagram", "lines": ["Facade structure"], "diagram": 10},
    text_page(1),
    POLL,
]


@pytest.fixture
def make_parser(session_factory, storage):
    def make(vision: FakeVision) -> Parser:
        return Parser(session_factory, storage, lambda: vision, concurrency=2)

    return make


@pytest.fixture
def slides_file(db, storage, subject) -> File:
    result = ingest_upload(db, storage, subject, "Week3_Lecture5.pdf", "application/pdf",
                           make_deck(DECK), max_bytes=50_000_000)  # fmt: skip
    assert result.file.type == "slides"
    return result.file


def slides(db, file: File) -> list[Slide]:
    db.expire_all()
    return list(db.scalars(select(Slide).where(Slide.file_id == file.id).order_by(Slide.page_no)))


def test_parse_routes_dedupes_and_records(db, storage, slides_file, make_parser):
    vision = FakeVision()
    make_parser(vision).parse(slides_file.id)

    db.refresh(slides_file)
    assert slides_file.status == "parsed", slides_file.status_detail
    assert slides_file.page_count == 8
    rows = slides(db, slides_file)
    assert [r.route for r in rows] == [
        "text", "duplicate", "duplicate", "vision", "vision", "vision", "text", "duplicate",
    ]  # fmt: skip
    assert len(vision.calls) == 3
    by_page = {r.page_no: r for r in rows}
    assert by_page[2].duplicate_of == by_page[4].id
    assert by_page[8].duplicate_of == by_page[5].id
    # The final build step's request carries the earlier steps' captions.
    keeper_call = next(c for c in vision.calls if "After 30s" in c["text"])
    assert keeper_call["captions"] == ["It starts Red Standing", "Button pressed: Green Walking"]
    # Every page has an image; each vision page records tokens and cost.
    assert all(r.image_path in storage.objects for r in rows)
    assert by_page[4].input_tokens == 3800 and by_page[4].cost_usd == pytest.approx(0.0126)
    assert by_page[4].content_json["visuals"][0]["key_facts"] == ["Red --button--> Green"]
    assert by_page[1].content_json["content_markdown"].startswith("# Topic 0")
    assert "this parse $0.04" in slides_file.status_detail


def test_reparse_is_free(db, storage, slides_file, make_parser):
    make_parser(FakeVision()).parse(slides_file.id)
    puts = storage.puts

    second = FakeVision()
    make_parser(second).parse(slides_file.id)

    assert second.calls == []  # no model calls
    assert storage.puts == puts  # no images re-uploaded
    rows = slides(db, slides_file)
    vision_rows = [r for r in rows if r.route == "vision"]
    assert all(r.cached for r in vision_rows)
    assert "this parse $0.00" in db.get(File, slides_file.id).status_detail


def test_cache_survives_delete_and_reupload(db, storage, subject, make_parser):
    from app.services.files import delete_file

    data = make_deck(DECK)  # same bytes both times (each generated PDF differs slightly)
    first = ingest_upload(db, storage, subject, "L5.pdf", "application/pdf", data,
                          max_bytes=50_000_000).file  # fmt: skip
    make_parser(FakeVision()).parse(first.id)
    delete_file(db, storage, first)
    assert not any(p.startswith("pages/") for p in storage.objects)  # images cleaned up

    again = ingest_upload(db, storage, subject, "copy.pdf", "application/pdf", data,
                          max_bytes=50_000_000).file  # fmt: skip
    vision = FakeVision()
    make_parser(vision).parse(again.id)
    assert vision.calls == []
    db.refresh(again)
    assert again.status == "parsed"


def test_prompt_or_model_change_reruns_every_request(db, slides_file, make_parser):
    make_parser(FakeVision()).parse(slides_file.id)

    edited = FakeVision()
    edited.prompt = "fake prompt v2"  # shared by every request
    make_parser(edited).parse(slides_file.id)
    assert len(edited.calls) == 3

    other = FakeVision()
    other.model = "gpt-6-luna"
    make_parser(other).parse(slides_file.id)
    assert len(other.calls) == 3


def test_only_changed_requests_rerun(db, storage, subject, make_parser):
    """A new deck where one vision page's text changed: only that page is paid for,
    even though the file (and its sha256) is different."""
    first = ingest_upload(db, storage, subject, "L5.pdf", "application/pdf", make_deck(DECK),
                          max_bytes=50_000_000).file  # fmt: skip
    make_parser(FakeVision()).parse(first.id)

    edited_deck = [dict(p) for p in DECK]
    edited_deck[5] = {**DECK[5], "lines": ["Facade structure (revised)"]}  # the big diagram
    second = ingest_upload(db, storage, subject, "L5 v2.pdf", "application/pdf",
                           make_deck(edited_deck), max_bytes=50_000_000).file  # fmt: skip
    assert second.sha256 != first.sha256

    vision = FakeVision()
    make_parser(vision).parse(second.id)
    assert len(vision.calls) == 1
    assert "Facade structure (revised)" in vision.calls[0]["text"]
    rows = {r.page_no: r for r in slides(db, second)}
    assert [n for n, r in rows.items() if r.route == "vision" and not r.cached] == [6]


def test_slide_rows_record_request_hash_and_prompt_version(db, slides_file, make_parser):
    make_parser(FakeVision()).parse(slides_file.id)
    vision_rows = [r for r in slides(db, slides_file) if r.route == "vision"]
    assert all(len(r.request_hash) == 64 for r in vision_rows)
    assert {r.prompt_version for r in vision_rows} == {vision_module.PROMPT_VERSION}
    stored = db.scalars(select(LlmCall)).all()
    assert {c.request_hash for c in stored} == {r.request_hash for r in vision_rows}
    assert all(c.kind == "slide_vision" and c.provider == "fake" for c in stored)


def test_failed_pages_fail_the_file_but_keep_finished_pages(db, storage, subject, make_parser):
    deck = [
        text_page(0),
        {"title": "OK", "lines": ["fine"], "diagram": 10},
        {"title": "Broken", "lines": ["FAIL here"], "diagram": 10},
        text_page(1),
        text_page(2),
    ]
    file = ingest_upload(db, storage, subject, "Lecture 9.pdf", "application/pdf",
                         make_deck(deck), max_bytes=50_000_000).file  # fmt: skip

    make_parser(FakeVision()).parse(file.id)
    db.refresh(file)
    assert file.status == "failed"
    assert "1 of 2 vision pages failed" in file.status_detail
    assert "simulated API error" in file.status_detail
    assert db.scalar(select(LlmCall).where(LlmCall.kind == "slide_vision")) is not None

    # Retrying only pays for the page that failed.
    retry = FakeVision()
    make_parser(retry).parse(file.id)
    assert len(retry.calls) == 1


def test_refusal_falls_back_to_text_path(db, storage, subject, make_parser):
    deck = [text_page(0), {"title": "Sensitive", "lines": ["REFUSE me"], "diagram": 10},
            text_page(1), text_page(2), text_page(3)]  # fmt: skip
    file = ingest_upload(db, storage, subject, "Lecture 2.pdf", "application/pdf",
                         make_deck(deck), max_bytes=50_000_000).file  # fmt: skip
    make_parser(FakeVision()).parse(file.id)
    row = slides(db, file)[1]
    assert row.route == "text"
    assert "model refused (not allowed)" in row.route_reason
    assert row.content_json["title"] == "Sensitive"


def test_pptx_without_libreoffice_fails_clearly(db, storage, subject, make_parser, monkeypatch):
    import io

    from pptx import Presentation

    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[0]).shapes.title.text = "Lecture 4"
    buf = io.BytesIO()
    prs.save(buf)
    file = ingest_upload(db, storage, subject, "Lecture 4 - GRASP.pptx", None, buf.getvalue(),
                         max_bytes=50_000_000).file  # fmt: skip
    monkeypatch.setattr("app.parse.document.find_soffice", lambda: None)

    make_parser(FakeVision()).parse(file.id)
    db.refresh(file)
    assert file.status == "failed"
    assert "LibreOffice" in file.status_detail and "PDF export" in file.status_detail


def test_missing_file_is_recorded_not_raised(db, make_parser):
    import uuid

    make_parser(FakeVision()).parse(uuid.uuid4())  # must not raise


def test_subject_is_untouched_by_parse(db, slides_file, make_parser):
    make_parser(FakeVision()).parse(slides_file.id)
    assert db.scalar(select(Subject)).name == "Machine Learning"


def test_deck_title_comes_from_parsed_first_page(db, storage, subject, make_parser):
    deck = [{"title": "GRASP: OO Design with Responsibilities", "lines": ["Lecturer: Dr X"]},
            {"title": "Learning Objectives", "lines": ["• Understand GRASP", "• Apply it",
                                                       "• And more", "• Even more text"]},
            text_page(1), text_page(2), text_page(3)]  # fmt: skip
    file = ingest_upload(db, storage, subject, "Lecture 4.pdf", "application/pdf",
                         make_deck(deck), max_bytes=50_000_000).file  # fmt: skip
    make_parser(FakeVision()).parse(file.id)
    db.refresh(file)
    assert file.title == "GRASP: OO Design with Responsibilities"
