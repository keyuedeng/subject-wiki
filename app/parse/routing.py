"""Decide per page: vision model or text path. Pure; thresholds tuned on real decks.

All measurements exclude the deck template (see document.py).
"""

from dataclasses import dataclass
from typing import Literal

from app.parse.document import PageInfo

Route = Literal["text", "vision"]

IMAGE_AREA = 0.08  # non-template images covering >= 8% of the page (smaller ones are icons)
MANY_PATHS = 20  # a diagram drawn as vector shapes (UML from PowerPoint/draw.io)
SOME_PATHS = 5  # ...or fewer shapes,
PATH_AREA = 0.15  # spread over >= 15% of the page (a small diagram)
EMPTY_TEXT = 10  # characters: below this the text layer is effectively missing


@dataclass(frozen=True)
class Decision:
    route: Route
    reason: str


def route_page(page: PageInfo) -> Decision:
    reasons = []
    if page.image_area >= IMAGE_AREA:
        n = page.image_count
        reasons.append(f"{n} image{'s' if n != 1 else ''} covering {page.image_area:.0%} of page")
    if page.drawing_count >= MANY_PATHS:
        reasons.append(f"{page.drawing_count} vector paths (diagram)")
    elif page.drawing_count >= SOME_PATHS and page.drawing_area >= PATH_AREA:
        reasons.append(
            f"{page.drawing_count} vector paths over {page.drawing_area:.0%} of page (diagram)"
        )
    if page.math_font:
        reasons.append("maths fonts (equations need LaTeX)")
    if reasons:
        return Decision("vision", "; ".join(reasons))

    if page.body_chars < EMPTY_TEXT:
        return Decision("text", "no text and no significant visuals")
    return Decision("text", f"{page.body_chars} characters, no significant visuals")
