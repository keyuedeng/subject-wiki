"""Collapse build slides (progressive reveals) and repeated pages. Pure.

A page is a *build step* of the next page when they share a title and either
- its text lines are a prefix/subset of the next page's (bullets revealed one by one), or
- its visual elements are a strict subset of the next page's and most of its lines
  carry over (a diagram growing while the caption changes).
Runs of build steps point at their last, most complete page. Separately, a page whose
content repeats an earlier page anywhere in the deck (e.g. the same poll slide shown
four times) points at the first occurrence.
"""

from dataclasses import dataclass

from app.parse.document import PageInfo

MIN_LINE_CARRYOVER = 0.5


@dataclass(frozen=True)
class Duplicate:
    page_no: int
    of: int  # the page kept
    reason: str


def _lines(page: PageInfo) -> list[str]:
    return [" ".join(t.split()).lower() for t in page.body_lines]


def _visuals(page: PageInfo) -> frozenset:
    return page.drawing_sigs | page.image_sigs


def _is_prefix(a: list[str], b: list[str]) -> bool:
    return len(a) <= len(b) and b[: len(a)] == a


def build_reason(page: PageInfo, nxt: PageInfo) -> str | None:
    """Why `page` is an earlier build step of `nxt`, or None."""
    if (page.title or "").lower() != (nxt.title or "").lower():
        return None
    a, b = _lines(page), _lines(nxt)
    va, vb = _visuals(page), _visuals(nxt)
    if a == b and va == vb:
        return "identical to the next page"
    if a == b and va < vb:
        return "same text; next page adds to the visual"
    if a != b and (_is_prefix(a, b) or set(a) < set(b)) and va <= vb:
        return "text revealed further on the next page"
    if va and va < vb:
        carried = sum(line in set(b) for line in a) / len(a) if a else 1.0
        if carried >= MIN_LINE_CARRYOVER:
            return "diagram built up further on the next page"
    return None


def _content_key(page: PageInfo) -> tuple | None:
    lines, visuals = tuple(_lines(page)), _visuals(page)
    if not lines and not visuals:
        return None  # blank pages aren't "the same content"
    return (lines, visuals)


def find_duplicates(pages: list[PageInfo]) -> list[Duplicate]:
    by_page: dict[int, Duplicate] = {}

    # Build runs: walk backwards so each step points at the end of its run.
    keeper: dict[int, int] = {}
    for i in range(len(pages) - 2, -1, -1):
        reason = build_reason(pages[i], pages[i + 1])
        if reason:
            nxt = pages[i + 1].page_no
            target = keeper.get(nxt, nxt)
            keeper[pages[i].page_no] = target
            by_page[pages[i].page_no] = Duplicate(pages[i].page_no, target, reason)

    # Repeats anywhere in the deck point at the first occurrence.
    first_seen: dict[tuple, int] = {}
    for page in pages:
        if page.page_no in by_page:
            continue
        key = _content_key(page)
        if key is None:
            continue
        if key in first_seen:
            by_page[page.page_no] = Duplicate(
                page.page_no, first_seen[key], f"repeats page {first_seen[key]}"
            )
        else:
            first_seen[key] = page.page_no
    return [by_page[n] for n in sorted(by_page)]
