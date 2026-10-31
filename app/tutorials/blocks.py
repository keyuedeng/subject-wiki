"""A tutorial sheet as an ordered list of blocks: headings, text, code and figures.

- PDF (and DOCX, converted to PDF): lines from the measured text layer (running headers,
  footers and page numbers already removed), in reading order. A line is a heading when
  it's set clearly larger than the body text, or bold and short, or when it names an
  exercise ("Exercise One", "Question 3", "Q2", "Task 1"). Figures come from the vision
  call's descriptions, placed where the page's largest image or diagram sits.
- Notebook (.ipynb): markdown `#` lines are headings, other markdown is text, code cells
  are code. Outputs are dropped.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass

import pymupdf

from app.parse.document import Deck, Line

HEADING_SCALE = 1.15  # a heading is set at least this much larger than the body text
MAX_HEADING_CHARS = 120
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20,
}  # fmt: skip
_NUM = r"(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")"
# "Exercise One - Determining Experts", "Question 3:", "Q2.", "Task 1", "Activity 4"
EXERCISE_RE = re.compile(
    r"^(?:exercise|question|task|problem|activity|challenge|q)\s*\.?\s*" + _NUM + r"\b",
    re.IGNORECASE,
)
LEADING_NUMBER_RE = re.compile(r"^(\d{1,2})(?:[.)]|\s)\s*\S")
LIST_START_RE = re.compile(r"^\s*(?:[•●▪■◦○‣∙·\-–—*]|\d{1,2}[.)]|\(?[a-z]\)|\(?[ivx]+\))\s+")
SENTENCE_END = (".", ",", ";")
# Private-use glyphs: Word's Symbol-font bullets ("\uf0b7") come through as these.
PRIVATE_USE_RE = re.compile("[\ue000-\uf8ff]")


def _clean(text: str) -> str:
    return " ".join(PRIVATE_USE_RE.sub(" ", text).split())


@dataclass
class Block:
    kind: str  # heading, text, code, figure
    text: str
    page: int | None = None
    level: int = 0  # headings: 1 = largest
    exercise: int | None = None  # headings naming an exercise: its number


def exercise_number(text: str) -> int | None:
    m = EXERCISE_RE.match(text.strip().strip("*#_ "))
    if m is None:
        return None
    raw = m.group(1).lower()
    return int(raw) if raw.isdigit() else NUMBER_WORDS[raw]


# PDF


def _body_size(lines: list[Line]) -> float:
    sizes: Counter = Counter()
    for ln in lines:
        sizes[round(ln.size * 2) / 2] += len(ln.text)
    return sizes.most_common(1)[0][0] if sizes else 10.0


def _is_heading(ln: Line, body: float) -> bool:
    text = ln.text.strip()
    if not text or len(text) > MAX_HEADING_CHARS:
        return False
    if exercise_number(text) is not None and len(text) <= 90:
        return True
    if ln.size >= body * HEADING_SCALE and not ln.mono:
        return True
    return ln.bold and ln.size >= body - 0.5 and len(text) <= 80 and not text.endswith(SENTENCE_END)


def _figure_y(page: pymupdf.Page) -> float:
    """Where the page's main visual is: its largest image, else its vector drawings."""
    images = [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
    images = [r for r in images if r.get_area() > 0.02 * page.rect.get_area()]
    if images:
        return max(images, key=lambda r: r.get_area()).y0
    draws = [d["rect"] for d in page.get_drawings() if d["rect"].get_area() > 4]
    if draws:
        return min(r.y0 for r in draws)
    return page.rect.height


def pdf_blocks(deck: Deck, figures: dict[int, list[str]]) -> list[Block]:
    """`figures`: page number -> figure descriptions from the vision call."""
    all_lines = [ln for p in deck.pages for ln in p.lines]
    body = _body_size(all_lines)
    heading_sizes = sorted(
        {round(ln.size * 2) / 2 for ln in all_lines if _is_heading(ln, body)}, reverse=True
    )
    level_of = {s: i + 1 for i, s in enumerate(heading_sizes)}

    with pymupdf.open(stream=deck.pdf, filetype="pdf") as doc:
        figure_y = {n: _figure_y(doc[n - 1]) for n in figures}

    blocks: list[Block] = []
    for page in deck.pages:
        n = page.page_no
        pending = list(figures.get(n, []))
        prev: Line | None = None
        for ln in sorted(page.lines, key=lambda ln: (ln.y0, ln.x0)):
            if pending and ln.y0 >= figure_y[n]:
                blocks += [Block("figure", f, n) for f in pending]
                pending = []
            text = _clean(ln.text)
            if not text:
                continue
            if _is_heading(ln, body):
                text = text.lstrip(":;,.-–— ").strip() or text
                last = blocks[-1] if blocks else None
                same = (
                    last is not None and last.kind == "heading" and last.page == n and prev
                    and abs(prev.size - ln.size) < 0.5 and ln.y0 - prev.y0 < 2 * ln.size
                )  # fmt: skip
                if same and last.exercise is None and exercise_number(text) is None:
                    last.text = f"{last.text} {text}"  # a heading wrapped over two lines
                else:
                    blocks.append(Block("heading", text, n, level_of[round(ln.size * 2) / 2],
                                        exercise_number(text)))  # fmt: skip
            elif ln.mono:
                if blocks and blocks[-1].kind == "code" and blocks[-1].page == n:
                    blocks[-1].text += "\n" + ln.text
                else:
                    blocks.append(Block("code", ln.text, n))
            else:
                last = blocks[-1] if blocks else None
                gap = prev is not None and ln.y0 - prev.y0 > 1.8 * ln.size
                if (last is not None and last.kind == "text" and last.page == n and not gap
                        and not LIST_START_RE.match(text)):  # fmt: skip
                    last.text = _join(last.text, text)
                else:
                    blocks.append(Block("text", text, n))
            prev = ln
        blocks += [Block("figure", f, n) for f in pending]
    return blocks


def _join(a: str, b: str) -> str:
    if a.endswith("-") and b[:1].islower():  # a hyphen at a line break
        return a[:-1] + b
    return f"{a} {b}"


# Notebook


def ipynb_blocks(data: bytes) -> list[Block]:
    cells = json.loads(data.decode("utf-8-sig")).get("cells") or []
    blocks: list[Block] = []
    for cell in cells:
        source = cell.get("source") or ""
        source = "".join(source) if isinstance(source, list) else str(source)
        if not source.strip():
            continue
        if cell.get("cell_type") == "code":
            blocks.append(Block("code", source.rstrip()))
            continue
        para: list[str] = []
        for line in source.splitlines():
            stripped = line.strip()
            heading = re.match(r"^(#{1,6})\s+(.*)", stripped)
            # "**Question 1** Crack the hash": a bold exercise name opens a section.
            bold = re.match(r"^\*\*(.+?)\*\*[:.]?\s*(.*)$", stripped)
            if bold and exercise_number(bold.group(1)) is None:
                bold = None
            if heading or bold:
                if para:
                    blocks.append(Block("text", "\n".join(para)))
                    para = []
                text = (heading.group(2) if heading else bold.group(1)).strip("*_ ")
                level = len(heading.group(1)) if heading else 7
                blocks.append(Block("heading", text, None, level, exercise_number(text)))
                if bold and bold.group(2):
                    para.append(bold.group(2))
            elif stripped:
                para.append(line)
            elif para:
                blocks.append(Block("text", "\n".join(para)))
                para = []
        if para:
            blocks.append(Block("text", "\n".join(para)))
    return blocks
