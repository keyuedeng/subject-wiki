"""Citable sources: a chunk plus its file, with the label a student sees and the link that
opens it. Pure (built from rows by app/services/ask.py).

Labels: "Week 4 · slide 14", "Week 4 lecturer notes · slide 3", "Week 4 lecture · 23:40".
When a week has more than one deck, the lecture number is added ("Week 4 L5 · slide 14").
"""

import uuid
from dataclasses import dataclass

KIND_NAMES = {
    "slide": "Slide",
    "notes": "Lecturer notes",
    "transcript": "Lecture transcript",
    "tutorial": "Tutorial",
}


def clock(ms: int | None) -> str:
    if ms is None:
        return "–"
    s = ms // 1000
    return (
        f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
    )


@dataclass(frozen=True)
class SourceRef:
    chunk_id: uuid.UUID
    source_type: str
    file_id: uuid.UUID
    file_title: str
    week: int | None
    lecture: int | None
    position: int
    title: str
    body: str
    meta: dict
    multi_lecture_week: bool = False  # the week has more than one deck

    @property
    def kind(self) -> str:
        return KIND_NAMES.get(self.source_type, self.source_type)

    def _week(self, transcript: bool = False) -> str:
        lecture = self.lecture if self.multi_lecture_week else None
        if self.week is None:
            return f"Lecture {self.lecture}" if self.lecture is not None else self.file_title
        if transcript:
            return f"Week {self.week} lecture" + (f" {lecture}" if lecture is not None else "")
        return f"Week {self.week}" + (f" L{lecture}" if lecture is not None else "")

    @property
    def page(self) -> int | None:
        return self.meta.get("page_no")

    @property
    def label(self) -> str:
        if self.source_type == "slide":
            return f"{self._week()} · slide {self.page}"
        if self.source_type == "notes":
            return f"{self._week()} lecturer notes · slide {self.page}"
        if self.source_type == "transcript":
            where = self._week(transcript=True)
            if self.week is None and self.lecture is None:
                where += " lecture"
            start = self.meta.get("start_ms")
            moment = clock(start) if start is not None else f"part {self.position + 1}"
            return f"{where} · {moment}"
        return f"{self._week()} · {self.title}"

    @property
    def href(self) -> str:
        if self.source_type in ("slide", "notes"):
            anchor = "#notes" if self.source_type == "notes" else ""
            return f"/slides/{self.meta['slide_id']}{anchor}"
        if self.source_type == "transcript":
            return f"/files/{self.file_id}/transcript#seg-{self.meta.get('idx', self.position)}"
        return f"/files/{self.file_id}"

    @property
    def flags(self) -> list[str]:
        return list(self.meta.get("flags") or [])

    def describe(self) -> str:
        """One header line for the model: what the source is and where it's from."""
        week = f"Week {self.week}, " if self.week is not None else ""
        if self.source_type == "slide":
            head = f'Slide {self.page} of "{self.file_title}" ({week}lecture deck)'
        elif self.source_type == "notes":
            head = f'Lecturer\'s speaker notes for slide {self.page} of "{self.file_title}"'
        elif self.source_type == "transcript":
            start = self.meta.get("start_ms")
            at = f" at {clock(start)}" if start is not None else ""
            where = [f'"{self.file_title}"']
            if self.week is not None:
                where.append(f"Week {self.week}")
            head = f"Lecture transcript{at} ({', '.join(where)})"
            if self.title:
                head += f", while the slide '{self.title}' was shown"
        else:
            head = f'{self.kind}: "{self.file_title}"'
        if self.source_type != "transcript" and self.title:
            head += f" — {self.title}"
        notes = []
        if "exam_hint" in self.flags:
            notes.append("the lecturer said something here is likely to be examined")
        if "worked_example" in self.flags:
            notes.append("worked example")
        return head + (f" [{'; '.join(notes)}]" if notes else "")


def span_label(first: SourceRef, last: SourceRef) -> str:
    """A run of neighbouring sources from one file: "Week 4 · slides 12–16",
    "Week 4 lecture · 23:40–26:10"."""
    if first.chunk_id == last.chunk_id:
        return first.label
    if first.source_type == "slide":
        return f"{first._week()} · slides {first.page}–{last.page}"
    if first.source_type == "notes":
        return f"{first._week()} lecturer notes · slides {first.page}–{last.page}"
    if first.source_type == "transcript":
        start, end = first.meta.get("start_ms"), last.meta.get("end_ms")
        if start is not None and end is not None:
            return first.label.rsplit(" · ", 1)[0] + f" · {clock(start)}–{clock(end)}"
    return f"{first.label} – {last.label.rsplit(' · ', 1)[-1]}"


def span_short(first: SourceRef, last: SourceRef) -> str:
    """The same span without the week, for rows already grouped under a week:
    "Slides 18–22", "Slide 26", "Lecturer notes · slide 3", "40:44–43:07"."""
    same = first.chunk_id == last.chunk_id
    if first.source_type == "slide":
        return f"Slide {first.page}" if same else f"Slides {first.page}–{last.page}"
    if first.source_type == "notes":
        pages = f"slide {first.page}" if same else f"slides {first.page}–{last.page}"
        return f"Lecturer notes · {pages}"
    if first.source_type == "transcript":
        start, end = first.meta.get("start_ms"), last.meta.get("end_ms")
        if start is None:
            return f"Lecture · part {first.position + 1}"
        if same:
            return clock(start)
        return f"{clock(start)}–{clock(end)}" if end is not None else clock(start)
    return first.title or first.kind
