"""Human-readable titles for files.

- `document_title` picks the topic from a document's own large text (stored at upload).
- `filename_title` cleans a filename for display when no document title exists.
- `display_title` combines both with a label from the type and numbers as last resort.

Week/lecture/tutorial numbers are stripped from titles because the UI shows them
separately (week rows, "L5" badges).
"""

import re

from app.classify import patterns as p
from app.classify.types import FileType

MIN_LEN, MAX_LEN = 3, 90

_NUMBER_TOKENS = re.compile(
    r"\b(?:weeks?|wk|w|lectures?|lect|lec|l|tutorials?|tutes?|tut|workshops?|ws|labs?|"
    r"topic|module|part|session)\s?0*\d{1,2}[a-z]?\b",
    re.IGNORECASE,
)
_COPY_SUFFIX = re.compile(r"(?:\s*\(\d+\)|\s*[-_ ]\s*copy(?:\s*\d+)?|\s+\[\d+\])+$", re.IGNORECASE)
_VERSION_SUFFIX = re.compile(r"(?:^|[\s_\-]+)v\d+(?:\.\d+)*$", re.IGNORECASE)
_LEADING_NUMBER = re.compile(r"^\s*\d{1,3}(?:\.\d{1,2})?\s*[.\-_:)]?\s+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z]{2,})")
_LETTER_DIGIT = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_SEPARATORS = re.compile(r"[_\-.+]+|\s+")
_EDGE_PUNCT = re.compile(r"^[\s:;,\-–—|·.]+|[\s:;,\-–—|·.]+$")
_BOILERPLATE = re.compile(
    r"universit|faculty|school of|department|copyright|©|commonwealth|warning|"
    r"this material|reproduced|semester|^slide \d|^page \d|\bpage \d+ of\b|https?://|www\.|@|"
    r"^\d{1,2}[/.]\d{1,2}[/.]\d{2,4}$|^(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*day\b|"
    r"^(?:lecturer|presented by|prepared by|dr\.?|prof\.?)\b",
    re.IGNORECASE,
)

FALLBACK = {
    FileType.SLIDES: "Lecture slides",
    FileType.TRANSCRIPT: "Transcript",
    FileType.TUTORIAL: "Tutorial",
    FileType.SOLUTIONS: "Solutions",
    FileType.PAST_PAPER: "Past paper",
    FileType.OUTLINE: "Subject outline",
    FileType.READING: "Reading",
    FileType.OTHER: "Untitled",
}


def _strip_subject(text: str, subject_code: str | None, subject_name: str | None) -> str:
    if subject_code and subject_code.strip():
        text = re.sub(p._code_pattern(subject_code).pattern, " ", text, flags=re.IGNORECASE)
    if subject_name and len(subject_name.strip()) >= MIN_LEN:
        text = re.sub(re.escape(subject_name.strip()), " ", text, flags=re.IGNORECASE)
    return text


# Small caps extracted from PDFs: "A PPLYING G O F D ESIGN" -> "APPLYING GOF DESIGN".
_SMALL_CAPS_SPLIT = re.compile(r"\b([A-Z]) (?=[A-Z]{2,}\b)")
_SPACED_CAPITALS = re.compile(r"\b[A-Z](?: [A-Z]\b)+")


def fix_small_caps(text: str) -> str:
    """Only for all-caps lines, which is how small caps come out of a PDF."""
    if text.upper() != text or not _SMALL_CAPS_SPLIT.search(text):
        return text
    text = _SMALL_CAPS_SPLIT.sub(r"\1", text)
    return _SPACED_CAPITALS.sub(lambda m: m.group(0).replace(" ", ""), text)


def _tidy(text: str) -> str:
    text = fix_small_caps(text)
    text = _NUMBER_TOKENS.sub(" ", text)
    text = " ".join(text.split())
    text = _EDGE_PUNCT.sub("", text)
    if text and text[0].islower():
        text = text[0].upper() + text[1:]
    return text


def _usable(text: str) -> bool:
    letters = sum(c.isalpha() for c in text)
    return MIN_LEN <= len(text) <= MAX_LEN and letters >= MIN_LEN


def document_title(
    candidates: tuple[str, ...] | list[str],
    subject_code: str | None = None,
    subject_name: str | None = None,
) -> str | None:
    """The first candidate that reads like a topic, not a code, a date or boilerplate."""
    for raw in candidates:
        raw = " ".join(raw.split())
        if not raw or _BOILERPLATE.search(raw):
            continue
        title = _tidy(_strip_subject(raw, subject_code, subject_name))
        if _usable(title) and not re.fullmatch(r"(?i)(?:lecture|week|tutorial|slides?)s?", title):
            return title
    return None


def filename_title(filename: str, subject_code: str | None = None) -> str | None:
    stem, _ = p.split_ext(filename)
    stem = _VERSION_SUFFIX.sub("", _COPY_SUFFIX.sub("", stem))
    stem = _strip_subject(stem, subject_code, None)
    stem = _CAMEL.sub(" ", stem)
    stem = _LETTER_DIGIT.sub(" ", stem)
    stem = _SEPARATORS.sub(" ", stem)
    stem = _LEADING_NUMBER.sub("", stem)
    title = _tidy(stem)
    return title if any(c.isalpha() for c in title) else None


def fallback_title(ftype: FileType, week: int | None, lecture: int | None) -> str:
    if ftype in (FileType.SLIDES, FileType.TRANSCRIPT) and lecture is not None:
        return f"Lecture {lecture}" + (" transcript" if ftype == FileType.TRANSCRIPT else "")
    if week is not None and ftype in (FileType.TUTORIAL, FileType.SOLUTIONS, FileType.SLIDES):
        return f"Week {week} {FALLBACK[ftype].lower()}"
    return FALLBACK[ftype]


def display_title(
    title: str | None,
    filename: str,
    ftype: FileType,
    week: int | None,
    lecture: int | None,
    subject_code: str | None = None,
) -> str:
    if title:
        return fix_small_caps(title)  # titles stored before the fix
    return filename_title(filename, subject_code) or fallback_title(ftype, week, lecture)
