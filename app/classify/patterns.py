"""Every regex and keyword table the classifier uses, in one place.

Filename patterns run on `normalize()`d names: lowercase, separators turned into
spaces, and letters split from digits ("COMP30027_Week3" -> "comp 30027 week 3").
"""

import re

from app.classify.types import FileType

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}  # fmt: skip
_NUM = r"(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")"

# "week 3", "wk 03", "w 3" (from "W03"), "weeks 1 3" (first of a range), "week three".
WEEK_RE = re.compile(r"\b(?:weeks?|wk|w)\s?0*" + _NUM + r"\b")
# "lecture 5", "lec 05", "lect 5", "l 5" (from "L5"), "lecture five".
LECTURE_RE = re.compile(r"\b(?:lectures?|lect|lec|l)\s?0*" + _NUM + r"\b")
# Tutorial/workshop numbers are reported in the reason but never used as a week.
TUTORIAL_NUM_RE = re.compile(
    r"\b(?:tutorials?|tutes?|tut|workshops?|ws|labs?|problem set|ps)\s?0*(\d{1,2})\b"
)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

# Filename keywords per type, checked in PRECEDENCE order: when several match,
# the first type in this list wins ("Tute 4 solutions" -> solutions).
FILENAME_KEYWORDS: list[tuple[FileType, re.Pattern[str]]] = [
    (
        FileType.SOLUTIONS,
        re.compile(
            r"\b(?:solutions?|soln?s?|sols|answers?|worked|marking (?:guide|scheme)|"
            r"model answers?)\b"
        ),
    ),
    (
        FileType.PAST_PAPER,
        re.compile(
            r"\b(?:exams?|finals|mid ?sem(?:ester)?|mid ?terms?|past papers?|"
            r"sample (?:exam|paper)|practice (?:exam|paper)|test papers?)\b"
        ),
    ),
    (FileType.TRANSCRIPT, re.compile(r"\b(?:transcripts?|captions?|subtitles?)\b")),
    (
        FileType.TUTORIAL,
        re.compile(
            r"\b(?:tutorials?|tutes?|tut|workshops?|labs?|problem sets?|worksheets?|"
            r"exercises?|practicals?|prac|questions)\b"
        ),
    ),
    (
        FileType.OUTLINE,
        re.compile(
            r"\b(?:outline|syllabus|handbook|(?:subject|course|unit) (?:guide|overview|info)|"
            r"assessment (?:guide|schedule|overview)|timetable|schedule)\b"
        ),
    ),
    (
        FileType.READING,
        re.compile(r"\b(?:readings?|chapter|ch \d+|textbook|article|paper by|notes on)\b"),
    ),
    (FileType.SLIDES, re.compile(r"\b(?:lectures?|lect|lec|slides?|deck|handouts?)\b")),
    # Known non-study-material files: confidently "other" rather than "nothing matched".
    (
        FileType.OTHER,
        re.compile(r"\b(?:assignments?|assessment task|project spec|announcements?|rubric)\b"),
    ),
]
PRECEDENCE = [t for t, _ in FILENAME_KEYWORDS]

# Keyword pairs that commonly appear together and don't signal confusion.
COMPATIBLE = {
    frozenset({FileType.SOLUTIONS, FileType.TUTORIAL}),
    frozenset({FileType.SOLUTIONS, FileType.PAST_PAPER}),
    frozenset({FileType.TRANSCRIPT, FileType.SLIDES}),
    frozenset({FileType.TUTORIAL, FileType.SLIDES}),  # "Lecture 3 exercises"
    frozenset({FileType.PAST_PAPER, FileType.TUTORIAL}),  # "exam questions"
}

# Content markers, searched in the lowercased first page / first lines of text.
# A type needs at least `min_hits` distinct markers to count.
CONTENT_MARKERS: list[tuple[FileType, int, re.Pattern[str]]] = [
    (
        FileType.PAST_PAPER,
        2,
        re.compile(
            r"reading time|writing time|total marks|this (?:paper|exam)|instructions to "
            r"candidates|student (?:id|number)|examination|exam duration|number of pages"
        ),
    ),
    (
        FileType.SOLUTIONS,
        1,
        re.compile(r"\bsolutions?\b|sample answers?|marking (?:guide|scheme)|model answers?"),
    ),
    (
        FileType.OUTLINE,
        2,
        re.compile(
            r"learning outcomes|subject coordinator|course coordinator|assessment|"
            r"contact hours|prerequisites?|weekly schedule|credit points"
        ),
    ),
    (
        FileType.TUTORIAL,
        2,
        re.compile(r"\btutorial\b|\bworkshop\b|\bexercises?\b|question 1\b|\bq1\b|\blab\b"),
    ),
    (
        FileType.READING,
        2,
        re.compile(
            r"\babstract\b|\breferences\b|\bdoi\b|et al\.|\bjournal\b|\bisbn\b|"
            r"\bproceedings\b|\bvolume\b"
        ),
    ),
]

# Transcripts.
TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?\b")
CUE_ARROW_RE = re.compile(r"-->")
FILLER_RE = re.compile(r"\b(?:um+|uh+|erm|okay|ok|yeah|alright|all right|gonna|you know)\b")
MIN_TIMESTAMP_LINES = 3
MIN_SPEECH_WORDS = 100
MIN_FILLERS = 3

# Accepted and rejected uploads.
ACCEPTED_EXTENSIONS = {"pdf", "pptx", "docx", "vtt", "srt", "txt"}
AUDIO_VIDEO_EXTENSIONS = {
    "mp3", "m4a", "wav", "aac", "flac", "ogg", "oga", "opus", "wma", "aiff",
    "mp4", "m4v", "mov", "mkv", "avi", "webm", "wmv", "flv", "mpeg", "mpg", "3gp",
}  # fmt: skip

_SEPARATORS_RE = re.compile(r"[_\-.+()\[\]{},;:&]+")
_LETTER_DIGIT_RE = re.compile(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])")
_SPACES_RE = re.compile(r"\s+")


def split_ext(filename: str) -> tuple[str, str]:
    stem, dot, ext = filename.rpartition(".")
    return (stem, ext.lower()) if dot and stem else (filename, "")


def _code_pattern(subject_code: str) -> re.Pattern[str]:
    """Match a subject code with or without a separator: COMP30027, comp 30027, COMP-30027."""
    code = re.sub(r"[\s_\-]", "", subject_code.lower())
    parts = re.split(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", code)
    return re.compile(r"[\s_\-]?".join(re.escape(p) for p in parts))


def normalize(name: str, subject_code: str | None = None) -> str:
    text = name.lower()
    if subject_code and subject_code.strip():
        # Remove the subject's own code first so its digits can't read as a week or lecture.
        text = _code_pattern(subject_code).sub(" ", text)
    text = _SEPARATORS_RE.sub(" ", text)
    text = _LETTER_DIGIT_RE.sub(" ", text)
    return _SPACES_RE.sub(" ", text).strip()


def to_int(token: str) -> int | None:
    return int(token) if token.isdigit() else NUMBER_WORDS.get(token)


def find_number(pattern: re.Pattern[str], text: str, max_value: int) -> int | None:
    for match in pattern.finditer(text):
        value = to_int(match.group(1))
        if value is not None and 0 <= value <= max_value:
            return value
    return None
