"""Parse buttons, the slide viewer and page images, with an inline queue and fake model."""

from concurrent.futures import Future

import pytest
from fastapi.testclient import TestClient

from app.jobs.worker import ParseQueue, get_queue
from app.main import app
from app.parse.pipeline import Parser
from tests.slide_fixtures import STATE_MACHINE_BUILD, make_deck
from tests.test_pipeline import FakeVision


class InlineExecutor:
    """Runs jobs immediately, so a request returns after the parse finished."""

    def submit(self, fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future

    def shutdown(self, **_):
        pass


@pytest.fixture
def vision():
    return FakeVision()


@pytest.fixture
def parse_client(client, session_factory, storage, vision):
    parser = Parser(session_factory, storage, lambda: vision)
    app.dependency_overrides[get_queue] = lambda: ParseQueue(parser, executor=InlineExecutor())
    return client


def text_page(i: int) -> dict:
    return {"title": f"Topic {i}", "lines": [f"• Point {i}.{j}" for j in range(3)]}


DECK = [text_page(0), *STATE_MACHINE_BUILD, text_page(1), text_page(2)]


def setup_subject(client: TestClient) -> tuple[str, str]:
    r = client.post("/subjects", data={"name": "SMD", "code": "SWEN30006", "weeks": "12"},
                    follow_redirects=False)  # fmt: skip
    sid = r.headers["location"].rsplit("/", 1)[1]
    up = client.post(
        f"/api/subjects/{sid}/files",
        files={"file": ("Week 6 Lecture 11 State Machines.pdf", make_deck(DECK))},
    ).json()
    return sid, up["file"]["id"]


def test_parse_button_then_viewer(parse_client, vision):
    sid, fid = setup_subject(parse_client)
    table = parse_client.get(f"/subjects/{sid}?view=table").text
    assert f'hx-post="/files/{fid}/parse?view=row"' in table
    assert "1 file is ready to process" in table

    row = parse_client.post(f"/files/{fid}/parse?view=row")
    assert row.status_code == 200 and "6 slides · parsed" in row.text
    assert f'href="/files/{fid}/slides"' in row.text
    assert len(vision.calls) == 1

    viewer = parse_client.get(f"/files/{fid}/slides?page=4")
    assert viewer.status_code == 200
    assert "6 pages" in viewer.text and "1 vision" in viewer.text
    assert "Red --button--&gt; Green" in viewer.text  # key facts, escaped
    assert f'src="/files/{fid}/pages/4.png"' in viewer.text

    dup = parse_client.get(f"/files/{fid}/slides?page=2").text
    assert "Go to page 4" in dup

    only = parse_client.get(f"/files/{fid}/slides?page=1&only=vision").text
    assert "Page 4 of 6" in only

    img = parse_client.get(f"/files/{fid}/pages/4.png")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert img.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_parse_all_and_reparse_cost_nothing(parse_client, vision):
    sid, fid = setup_subject(parse_client)
    r = parse_client.post(f"/api/subjects/{sid}/parse")
    assert r.status_code == 202 and r.json()["queued"] == 1

    again = parse_client.post(f"/api/files/{fid}/parse")
    assert again.status_code == 202
    assert len(vision.calls) == 1  # second parse used the cache
    viewer = parse_client.get(f"/files/{fid}/slides?page=4").text
    assert "This parse $0.00" in viewer and "1 of 1 vision pages cached" in viewer

    slides = parse_client.get(f"/api/files/{fid}/slides").json()
    assert [s["route"] for s in slides].count("vision") == 1


def test_only_slides_can_be_parsed(parse_client):
    sid, _ = setup_subject(parse_client)
    up = parse_client.post(
        f"/api/subjects/{sid}/files",
        files={"file": ("Week 2 Tutorial.pdf", make_deck([text_page(9)]))},
    )
    fid = up.json()["file"]["id"]
    assert parse_client.post(f"/api/files/{fid}/parse").status_code == 400


def test_viewer_markdown_cannot_inject_html(parse_client, session_factory):
    from app.db.models import Slide

    sid, fid = setup_subject(parse_client)
    parse_client.post(f"/api/files/{fid}/parse")
    with session_factory() as db:
        slide = db.query(Slide).filter_by(page_no=1).one()
        slide.content_json = {**slide.content_json, "content_markdown": "<script>x()</script>"}
        db.commit()
    page = parse_client.get(f"/files/{fid}/slides?page=1").text
    assert "<script>x()</script>" not in page


def test_uml_visibility_markers_are_not_nested_bullets():
    from app.web.routes import _render_markdown

    html = _render_markdown("- + getCode() : int\n- - secret : int\n- plain")
    assert "<li>+ getCode() : int</li>" in html
    assert "<li>- secret : int</li>" in html
    assert html.count("<ul>") == 1
