from app.parse.dedupe import find_duplicates
from app.parse.document import measure
from tests.slide_fixtures import BULLET_BUILD, POLL, STATE_MACHINE_BUILD, make_deck


def text_page(i: int) -> dict:
    return {"title": f"Topic {i}", "lines": [f"• Point {i}.{j}" for j in range(3)]}


def dups(specs: list[dict]) -> dict[int, tuple[int, str]]:
    pages = measure(make_deck(specs))
    return {d.page_no: (d.of, d.reason) for d in find_duplicates(pages)}


def test_bullet_build_collapses_to_last_page():
    found = dups([text_page(0), *BULLET_BUILD, text_page(1), text_page(2)])
    assert {n: of for n, (of, _) in found.items()} == {2: 4, 3: 4}
    assert "text revealed" in found[2][1]


def test_diagram_build_with_changing_caption_collapses():
    # Real case: SWEN30006 L6 pedestrian crossing, the caption changes at every step.
    found = dups([text_page(0), *STATE_MACHINE_BUILD, text_page(1), text_page(2)])
    assert {n: of for n, (of, _) in found.items()} == {2: 4, 3: 4}
    assert "diagram built up" in found[2][1]


def test_same_title_different_content_is_kept():
    specs = [
        text_page(0),
        {"title": "Example: Shape Decorator", "lines": ["Class diagram and Rectangle.java"],
         "diagram": 3},
        {"title": "Example: Shape Decorator", "lines": ["ShapeDecorator.java code"]},
        text_page(1),
        text_page(2),
    ]  # fmt: skip
    assert dups(specs) == {}


def test_repeated_page_anywhere_points_at_first_occurrence():
    specs = [POLL, text_page(0), text_page(1), POLL, text_page(2), POLL]
    found = dups(specs)
    assert found == {4: (1, "repeats page 1"), 6: (1, "repeats page 1")}


def test_identical_consecutive_pages():
    page = {"title": "Summary", "lines": ["• Point A", "• Point B"]}
    found = dups([text_page(0), page, page, text_page(1), text_page(2)])
    assert found == {2: (3, "identical to the next page")}


def test_page_with_fewer_lines_but_new_text_is_not_a_build():
    specs = [
        text_page(0),
        {"title": "Guidelines", "lines": ["• First point", "• Second point"]},
        {"title": "Guidelines", "lines": ["• Something else entirely"]},
        text_page(1),
        text_page(2),
    ]
    assert dups(specs) == {}
