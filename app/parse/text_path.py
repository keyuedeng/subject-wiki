"""Build SlideContent from the text layer alone (no model call). Pure.

- title: the largest text on the page
- bullets: lines starting with a bullet glyph or a number, indented relative to the
  first bullet of the list
- code: monospace lines, or runs of code-shaped lines, become code blocks
- wrapped lines are joined to the line they continue
"""

import re

from app.parse.document import Line, PageInfo
from app.parse.schema import CodeBlock, SlideContent

BULLET_RE = re.compile(r"^\s*([•●▪■◦○‣∙·\-–—*]|\d{1,2}[.)])\s+")
# Code set in a proportional font: statement endings, braces, annotations, calls, comments,
# assignments split across lines. A run needs MIN_CODE_RUN such lines in a row.
CODE_LINE_RE = re.compile(
    r"[;{}]\s*$|^\s*[{}]|^\s*@\w+|^\s*case\s+\w+\s*:|\w\([^)]*\)\s*[;{]?\s*$|=\s*(new)?\s*$"
    r"|^\s*(//|/\*|\*/)"
)
MIN_CODE_RUN = 2
INDENT_STEP = 18  # points per indent level
MAX_INDENT = 3


def text_content(page: PageInfo) -> SlideContent:
    title = page.title
    title_lines = page.title_lines
    body = [ln for ln in page.lines if ln not in title_lines]
    markdown, code = _render(body)
    return SlideContent(
        title=title,
        content_markdown=(f"# {title}\n\n" if title else "") + markdown,
        visuals=[],
        equations=[],
        code_blocks=code,
    )


def _code_flags(lines: list[Line]) -> list[bool]:
    candidate = [ln.mono or bool(CODE_LINE_RE.search(ln.text)) and not BULLET_RE.match(ln.text)
                 for ln in lines]  # fmt: skip
    flags = [False] * len(lines)
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and candidate[j]:
            j += 1
        if j - i >= MIN_CODE_RUN or any(ln.mono for ln in lines[i:j]):
            flags[i:j] = [True] * (j - i)
        i = max(j, i + 1)
    return flags


def _render(lines: list[Line]) -> tuple[str, list[CodeBlock]]:
    out: list[str] = []
    code: list[CodeBlock] = []
    code_run: list[str] = []
    list_x: float | None = None  # x of the first bullet in the current list
    prev: Line | None = None

    def flush_code() -> None:
        if code_run:
            block = "\n".join(code_run)
            code.append(CodeBlock(language=None, code=block))
            out.append(f"```\n{block}\n```")
            code_run.clear()

    for ln, is_code in zip(lines, _code_flags(lines), strict=True):
        if is_code:
            code_run.append(ln.text)
            prev, list_x = ln, None
            continue
        flush_code()
        if m := BULLET_RE.match(ln.text):
            if list_x is None or ln.x0 < list_x - 2:
                list_x = ln.x0
            level = min(MAX_INDENT, max(0, round((ln.x0 - list_x) / INDENT_STEP)))
            marker = "1." if m.group(1)[0].isdigit() else "-"
            out.append(f"{'  ' * level}{marker} {ln.text[m.end() :]}")
        elif prev is not None and out and not prev.mono and _continues(prev, ln):
            out[-1] += " " + ln.text
        else:
            out.append(f"**{ln.text}**" if ln.bold else ln.text)
            list_x = None
        prev = ln
    flush_code()
    return _join(out), code


def _continues(prev: Line, ln: Line) -> bool:
    """A wrapped line: same size, directly below, not left of the previous line, and
    either the previous line didn't end a sentence or this one starts lower-case."""
    gap = ln.y0 - prev.y0
    close_below = 0 < gap <= prev.size * 1.6
    ended = prev.text.rstrip().endswith((".", ":", "?", "!"))
    return (
        not prev.mono
        and abs(prev.size - ln.size) < 0.5
        and close_below
        and ln.x0 >= prev.x0 - 2
        and (not ended or ln.text[:1].islower())
    )


def _join(blocks: list[str]) -> str:
    text = []
    for i, block in enumerate(blocks):
        is_item = block.lstrip().startswith(("- ", "1. "))
        prev_item = i > 0 and blocks[i - 1].lstrip().startswith(("- ", "1. "))
        text.append(("\n" if is_item and prev_item else "\n\n") + block if i else block)
    return "".join(text).strip()
