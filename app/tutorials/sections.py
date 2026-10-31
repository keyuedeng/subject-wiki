"""Split a tutorial's blocks into sections by its own structure. Pure.

1. Split level: when some headings name exercises ("Exercise One", "Question 3"), split
   at every heading at their level or above, so each exercise is a section and so is
   each explanation between them. Otherwise split at the largest heading level that
   occurs more than once (a lone document title doesn't count).
2. Exercise sub-parts ((a), (b), 1., 2.), smaller headings, code and figures stay in
   their section.
3. A section with almost no text of its own (a part heading right before its first
   question, a title block) is merged into the next one. Exercises never are, however
   short.
4. A very long section is cut at its own sub-headings into "part 2", "part 3", keeping
   its number.
"""

from dataclasses import dataclass, field

from app.tutorials.blocks import LEADING_NUMBER_RE, Block

MIN_WORDS = 15
MAX_WORDS = 1200
PREAMBLE_TITLE = "Introduction"


@dataclass
class SectionSpec:
    title: str
    number: int | None  # an exercise's number, for pairing with solutions
    is_exercise: bool
    blocks: list[Block] = field(default_factory=list)
    part: int = 1

    @property
    def words(self) -> int:
        return sum(len(b.text.split()) for b in self.blocks if b.kind != "heading")

    @property
    def label(self) -> str:
        """ "Q3" for exercises (citations read "Week 6 tutorial · Q3"), else the title."""
        if self.is_exercise and self.number is not None:
            base = f"Q{self.number}"
        else:
            base = self.title if len(self.title) <= 48 else self.title[:47].rstrip() + "…"
        return base if self.part == 1 else f"{base} (part {self.part})"

    @property
    def pages(self) -> tuple[int | None, int | None]:
        nums = [b.page for b in self.blocks if b.page is not None]
        return (min(nums), max(nums)) if nums else (None, None)

    def markdown(self) -> str:
        out = []
        for b in self.blocks:
            if b.kind == "heading":
                out.append(f"{'#' * min(b.level + 1, 6)} {b.text}")
            elif b.kind == "code":
                out.append(f"```\n{b.text}\n```")
            elif b.kind == "figure":
                out.append("\n".join(f"> {line}" for line in b.text.splitlines()))
            else:
                out.append(b.text)
        return "\n\n".join(out)


def split_level(blocks: list[Block]) -> int | None:
    headings = [b for b in blocks if b.kind == "heading"]
    exercises = [b.level for b in headings if b.exercise is not None]
    if exercises:
        return max(exercises)
    counts: dict[int, int] = {}
    for b in headings:
        counts[b.level] = counts.get(b.level, 0) + 1
    repeated = sorted(level for level, n in counts.items() if n > 1)
    return repeated[0] if repeated else None


def split_sections(blocks: list[Block]) -> list[SectionSpec]:
    level = split_level(blocks)
    sections: list[SectionSpec] = []
    current = SectionSpec(PREAMBLE_TITLE, None, False)
    for b in blocks:
        if b.kind == "heading" and level is not None and b.level <= level:
            if current.blocks:
                sections.append(current)
            number = b.exercise
            if number is None and (m := LEADING_NUMBER_RE.match(b.text)):
                number = int(m.group(1))
            current = SectionSpec(b.text, number, b.exercise is not None)
        current.blocks.append(b)
    if current.blocks:
        sections.append(current)
    return _cut_long(_merge_short(sections))


def _merge_short(sections: list[SectionSpec]) -> list[SectionSpec]:
    out: list[SectionSpec] = []
    carry: SectionSpec | None = None  # a short section waiting to join the next one
    for s in sections:
        if carry is not None:
            title = s.title
            # A title block is named by the heading after it; a part heading prefixes it.
            if (not s.is_exercise and carry is not sections[0]
                    and carry.title not in (PREAMBLE_TITLE, s.title)):  # fmt: skip
                title = f"{carry.title}: {s.title}"
            s = SectionSpec(title, s.number, s.is_exercise, carry.blocks + s.blocks)
            carry = None
        if s.words < MIN_WORDS and not s.is_exercise:
            carry = s
            continue
        out.append(s)
    if carry is not None:  # a short last section joins the one before it
        if out:
            out[-1].blocks += carry.blocks
        else:
            out.append(carry)
    return out


def _cut_long(sections: list[SectionSpec]) -> list[SectionSpec]:
    out: list[SectionSpec] = []
    for s in sections:
        if s.words <= MAX_WORDS:
            out.append(s)
            continue
        parts: list[list[Block]] = [[]]
        words = 0
        for i, b in enumerate(s.blocks):
            if b.kind == "heading" and i > 0 and words >= MAX_WORDS // 2 and parts[-1]:
                parts.append([])
                words = 0
            parts[-1].append(b)
            if b.kind != "heading":
                words += len(b.text.split())
        for n, blocks in enumerate(parts, 1):
            out.append(SectionSpec(s.title, s.number, s.is_exercise, blocks, part=n))
    return out
