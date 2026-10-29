"""Measuring PDFs: text that can't be stored is cleaned at extraction."""

import pymupdf

from app.parse.document import clean_text, measure


def test_nul_glyphs_are_dropped_from_the_text_layer():
    # Some PDFs map glyphs with no Unicode value (here a bullet) to U+0000; Postgres can't
    # store it, which failed a whole 108-page deck (INFO30006 "Wk 2 - Authentication").
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Authentication", fontsize=24)
    page.insert_text((72, 120), "\x00", fontsize=12)  # a line that is only the bad glyph
    page.insert_text((72, 150), "Bullet \x00 point", fontsize=12)
    (info,) = measure(doc.tobytes())
    assert "\x00" not in info.text_raw and "Bullet  point" in info.text_raw
    assert [ln.text for ln in info.lines] == ["Authentication", "Bullet point"]


def test_clean_text_keeps_tabs_and_newlines():
    assert clean_text("a\x00b\tc\nd\r\x07e\x7f") == "ab\tc\nd\re"


def test_content_filter_stop_falls_back_like_a_refusal():
    # The filter cut off page 68 of the same deck (an xkcd comic being transcribed);
    # retrying can't help, so the page uses its text layer and the deck still parses.
    from app.llm.client import OutputFiltered, Usage
    from app.parse.vision import OpenAIVision

    vision = OpenAIVision(None, "gpt-6.1-sol", "medium")

    def filtered(**_):
        raise OutputFiltered(Usage(input_tokens=488))

    vision._provider.structured = filtered
    result = vision.describe(b"png", "Image: xkcd.com", [])
    assert result.content is None and "content filter" in result.refusal
    assert result.usage.input_tokens == 488
