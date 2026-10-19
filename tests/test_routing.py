import pytest

from app.parse.document import measure
from app.parse.routing import route_page
from tests.slide_fixtures import make_deck

TEXT = {"title": "Learning Objectives", "lines": ["• Understand state machines"] * 4}


def text_page(i: int) -> dict:
    return {"title": f"Topic {i}", "lines": [f"• Point {i}.{j}" for j in range(4)]}


PADDING = [text_page(i) for i in range(4)]


def routes(spec: dict) -> tuple[str, str]:
    # Pad with distinct plain pages so the logo, header bar and page numbers count as
    # template (repeated identical pages would make their text "template" too).
    pages = measure(make_deck([spec] + PADDING))
    decision = route_page(pages[0])
    return decision.route, decision.reason


@pytest.mark.parametrize(
    ("spec", "route", "reason"),
    [
        (TEXT, "text", "characters, no significant visuals"),
        ({"title": "Big diagram", "diagram": 12}, "vision", "vector paths (diagram)"),
        ({"title": "Small diagram", "small_diagram": True}, "vision", "of page (diagram)"),
        ({"title": "Photo", "image": 0.3}, "vision", "covering 30% of page"),
        ({"title": "Icon", "lines": ["Some text here"], "image": 0.02}, "text", "no significant"),
        ({"title": "Maths", "lines": ["Bayes"], "math": True}, "vision", "maths fonts"),
        ({"title": "Questions?"}, "text", "no significant visuals"),  # divider: no call
        ({"image": 0.4}, "vision", "covering 40% of page"),  # screenshot, no text layer
    ],
)
def test_route(spec, route, reason):
    got_route, got_reason = routes(spec)
    assert got_route == route, got_reason
    assert reason in got_reason


def test_template_logo_and_header_are_ignored():
    pages = measure(make_deck([text_page(i) for i in range(5)]))
    assert all(p.image_count == 0 and p.drawing_count == 0 for p in pages)
    assert all(route_page(p).route == "text" for p in pages)


def test_template_needs_enough_pages():
    # With only 2 pages, nothing can be told apart as template: the logo counts.
    pages = measure(make_deck([TEXT, TEXT]))
    assert pages[0].image_count == 1


def test_page_numbers_and_running_headers_are_not_body_text():
    header = "SWEN30006 Software Modelling"
    deck = [{**text_page(i), "lines": [header, *text_page(i)["lines"]]} for i in range(5)]
    page = measure(make_deck(deck))[2]
    assert page.title == "Topic 2"
    assert "3" not in page.body_lines
    assert header not in page.body_lines


def test_libreoffice_bullet_glyphs_are_not_maths():
    from app.parse.document import _is_maths

    assert not _is_maths({"font": "OpenSymbol", "text": ""})  # converted PPTX arrow
    assert not _is_maths({"font": "Symbol", "text": "• "})
    assert _is_maths({"font": "CMMI10", "text": "x"})
    assert _is_maths({"font": "Symbol", "text": "αβ"})
