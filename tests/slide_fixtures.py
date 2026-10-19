"""Generated slide decks for parsing tests. Every deck has a template: a logo image and
a header bar on every page, like real university decks."""

import pymupdf

W, H = 960, 540


def _png(w: int = 60, h: int = 60, colour: int = 200) -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.clear_with(colour)
    return pix.tobytes("png")


LOGO = _png(40, 40, 30)


def _template(page: pymupdf.Page) -> None:
    page.insert_image(pymupdf.Rect(20, 15, 60, 55), stream=LOGO)
    page.draw_rect(pymupdf.Rect(0, 0, W, 8), color=(0, 0, 0.5), fill=(0, 0, 0.5))
    page.insert_text((W - 40, H - 15), str(page.number + 1), fontsize=9)


def _title(page: pymupdf.Page, text: str) -> None:
    page.insert_text((80, 50), text, fontsize=28)


def _lines(page: pymupdf.Page, lines: list[str], y: float = 110, size: float = 16) -> None:
    for i, line in enumerate(lines):
        page.insert_text((80, y + i * size * 1.5), line, fontsize=size)


def _diagram(page: pymupdf.Page, boxes: int, origin=(150, 180)) -> None:
    """A UML-ish diagram: boxes in a row, joined by lines (each box = 1 path, each line 1)."""
    # Sized like a real slide diagram: 3 boxes span ~16% of the page.
    x0, y0 = origin
    for i in range(boxes):
        r = pymupdf.Rect(x0 + i * 180, y0, x0 + i * 180 + 90, y0 + 180)
        page.draw_rect(r, color=(0, 0, 0))
        if i:
            page.draw_line((r.x0 - 90, y0 + 90), (r.x0, y0 + 90), color=(0, 0, 0))


def make_deck(pages: list[dict]) -> bytes:
    """Each dict: title, lines, image (area 0-1), diagram (box count), small_diagram,
    math (bool)."""
    doc = pymupdf.open()
    for spec in pages:
        page = doc.new_page(width=W, height=H)
        _template(page)
        if spec.get("title"):
            _title(page, spec["title"])
        if spec.get("lines"):
            _lines(page, spec["lines"])
        if area := spec.get("image"):
            # 16:9 and centred, so the covered share of the page is exactly `area`.
            w, h = W * area**0.5, H * area**0.5
            rect = pymupdf.Rect((W - w) / 2, (H - h) / 2, (W + w) / 2, (H + h) / 2)
            page.insert_image(rect, stream=_png(160, 90), keep_proportion=False)
        if spec.get("diagram"):
            _diagram(page, spec["diagram"])
        if spec.get("small_diagram"):
            # 3 boxes spread across the page: few paths, large area
            for x in (100, 450, 800):
                page.draw_rect(pymupdf.Rect(x, 150, x + 60, 450), color=(0, 0, 0))
            page.draw_line((160, 300), (450, 300))
            page.draw_line((510, 300), (800, 300))
        if spec.get("math"):
            page.insert_text((80, 400), "a b g", fontname="Symbol", fontsize=18)
    return doc.tobytes()


STATE_MACHINE_BUILD = [
    {"title": "Example: Crossing Light", "lines": ["It starts Red Standing"], "diagram": 1},
    {"title": "Example: Crossing Light", "lines": ["Button pressed: Green Walking"], "diagram": 2},
    {"title": "Example: Crossing Light", "lines": ["After 30s: Flashing Red"], "diagram": 3},
]

BULLET_BUILD = [
    {"title": "Guidelines", "lines": ["• First point"]},
    {"title": "Guidelines", "lines": ["• First point", "• Second point"]},
    {"title": "Guidelines", "lines": ["• First point", "• Second point", "• Third point"]},
]

POLL = {"title": "", "image": 0.9}
