from app.parse.document import Line, PageInfo
from app.parse.text_path import text_content


def page(lines: list[Line]) -> PageInfo:
    return PageInfo(
        page_no=1, width=960, height=540, text_raw="", lines=lines, image_area=0,
        image_count=0, drawing_count=0, drawing_area=0,
    )  # fmt: skip


def line(text, y, x=80, size=16, mono=False, bold=False) -> Line:
    return Line(text=text, size=size, x0=x, y0=y, mono=mono, bold=bold)


def test_title_bullets_and_indent():
    content = text_content(page([
        line("Learning Objectives", 40, size=28),
        line("On completion you should:", 100),
        line("• Know state machines", 125, x=90),
        line("• Draw them", 150, x=90),
        line("– with guards", 175, x=115),
    ]))  # fmt: skip
    assert content.title == "Learning Objectives"
    assert content.content_markdown == (
        "# Learning Objectives\n\nOn completion you should:\n\n"
        "- Know state machines\n- Draw them\n  - with guards"
    )


def test_wrapped_bullet_is_joined():
    md = text_content(page([
        line("Title", 40, size=28),
        line("• A state machine is a behaviour model that", 100),
        line("captures dynamic behaviour", 120, x=95),
    ])).content_markdown  # fmt: skip
    assert "- A state machine is a behaviour model that captures dynamic behaviour" in md


def test_code_in_proportional_font_becomes_a_code_block():
    content = text_content(page([
        line("Example: BankAccountFacade", 40, size=28),
        line("public class BankAccountFacade {", 100),
        line("private int accountNumber;", 120),
        line("//Instantiate related objects", 140),
        line("}", 160),
    ]))  # fmt: skip
    assert len(content.code_blocks) == 1
    assert content.code_blocks[0].code.splitlines()[2] == "//Instantiate related objects"


def test_single_code_shaped_prose_line_is_not_code():
    content = text_content(page([
        line("Facade", 40, size=28),
        line("This object presents a unified interface (the facade).", 100),
    ]))  # fmt: skip
    assert content.code_blocks == []


def test_monospace_lines_are_code():
    content = text_content(page([line("Run", 40, size=28), line("make test", 100, mono=True)]))
    assert content.code_blocks[0].code == "make test"
