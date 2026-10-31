"""Short names for tutorial sheets in citations: "Workshop 4 · Q3". Pure.

The sheet's own number ("Workshop_04", "Tutorial 2 slides") gives "Workshop 4" /
"Tutorial 2"; numbers over 20 are ignored ("Tutorial '26W2" is the year). Without one the
sheet is "Week 3 tutorial", by its board week. When two sheets would share a name, both
use their titles instead.
"""

import re

from app.classify import patterns as p
from app.classify.titles import display_title
from app.classify.types import FileType
from app.db.models import File

MAX_SHEET_NUMBER = 20
KINDS = {
    "tutorial": "Tutorial", "tutorials": "Tutorial", "tute": "Tutorial", "tutes": "Tutorial",
    "tut": "Tutorial", "workshop": "Workshop", "workshops": "Workshop", "ws": "Workshop",
    "lab": "Lab", "labs": "Lab", "problem set": "Problem set", "ps": "Problem set",
}  # fmt: skip
_NUMBERED = re.compile(
    r"\b(tutorials?|tutes?|tut|workshops?|ws|labs?|problem set|ps)\s?0*(\d{1,2})\b"
)


def own_name(file: File, subject_code: str | None) -> str | None:
    for text in (file.filename, file.title or ""):
        m = _NUMBERED.search(p.normalize(p.split_ext(text)[0] if text == file.filename else text,
                                         subject_code))  # fmt: skip
        if m and 0 < int(m.group(2)) <= MAX_SHEET_NUMBER:
            return f"{KINDS[m.group(1)]} {int(m.group(2))}"
    return None


def sheet_names(files: list[File], subject_code: str | None) -> dict:
    """Tutorial file id -> its short name."""
    names = {}
    for f in files:
        if f.type != FileType.TUTORIAL.value:
            continue
        names[f.id] = own_name(f, subject_code) or (
            f"Week {f.week} tutorial" if f.week is not None else None
        )
    counts: dict[str, int] = {}
    for n in names.values():
        if n:
            counts[n] = counts.get(n, 0) + 1
    by_id = {f.id: f for f in files}
    for fid, n in names.items():
        if n is None or counts[n] > 1:
            f = by_id[fid]
            names[fid] = display_title(f.title, f.filename, FileType(f.type), f.week, f.lecture,
                                       subject_code)  # fmt: skip
    return names
