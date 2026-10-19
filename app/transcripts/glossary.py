"""Technical terms from a lecture's slides, for fixing speech-recognition errors. Pure.

No model call: terms are pulled from slide titles, content, visual descriptions and
key facts, and speaker notes, then ranked by how often they occur:
- acronyms (UML, GRASP, DCD)
- identifiers: CamelCase and lowerCamel (MonopolyGame, getAccountNumber)
- capitalised phrases not at the start of a sentence (Information Expert, Low Coupling)
- slide title phrases (split at ":" and "-")
"""

import re
from collections import Counter
from collections.abc import Iterable

MAX_TERMS = 300

ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,7}s?\b")
CAMEL_RE = re.compile(r"\b(?:[A-Z][a-z0-9]+){2,}\b|\b[a-z]+(?:[A-Z][a-z0-9]*)+\b")
PHRASE_RE = re.compile(r"(?<![.!?:]\s)(?<!^)\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b")
TITLE_SPLIT_RE = re.compile(r"\s*[:–—\-|]\s*")
NOISE = {
    "OK", "PDF", "THE", "AND", "FOR", "NOT", "YES", "NO", "II", "III", "IV", "AM", "PM",
    "The", "This", "That", "These", "Those", "What", "When", "Where", "Which", "Why", "How",
    "Example", "Examples", "Exercise", "Summary", "Remarks", "Learning", "Objectives",
    "Another", "More", "Some", "Each", "All", "For", "If", "In", "On", "To", "We", "You",
    "It", "A", "An", "Of", "Lecturer", "Semester", "University", "Melbourne", "Public",
    "Who", "Is", "Also", "Given", "Hard", "Problem", "Solution", "Revisited", "Revised",
    "Captured", "Week", "Another Example", "Discussion", "By", "So", "But", "Or", "Use",
}  # fmt: skip
LABEL_RE = re.compile(r"^[A-Z]\d{1,2}$")  # requirement/question labels: R5, Q2


def _texts(slide: dict) -> Iterable[str]:
    content = slide.get("content_json") or {}
    yield slide.get("title") or ""
    yield content.get("content_markdown") or ""
    for visual in content.get("visuals") or []:
        yield visual.get("description") or ""
        yield from visual.get("key_facts") or []
    yield slide.get("notes") or ""


def build_glossary(slides: list[dict], subject_code: str | None = None) -> list[str]:
    """`slides`: dicts with title, content_json and notes. Returns terms, most frequent
    first (ties alphabetical), so the same slides always give the same glossary."""
    counts: Counter[str] = Counter()
    for slide in slides:
        for text in _texts(slide):
            text = re.sub(r"[`*#>_\[\]()]", " ", text)
            for line in text.splitlines():
                counts.update(m.group(0) for m in ACRONYM_RE.finditer(line))
                counts.update(m.group(0) for m in CAMEL_RE.finditer(line))
                counts.update(m.group(1) for m in PHRASE_RE.finditer(line.strip()))
        for phrase in TITLE_SPLIT_RE.split(slide.get("title") or ""):
            phrase = phrase.strip(" .?!")
            if 1 <= len(phrase.split()) <= 5 and len(phrase) > 2:
                counts[phrase] += 2  # titles name the topics being taught
    if subject_code:
        counts[subject_code.upper()] += 1
    terms = [
        t for t in counts
        if t not in NOISE and not t.isdigit() and len(t) > 1 and not LABEL_RE.match(t)
    ]  # fmt: skip
    terms.sort(key=lambda t: (-counts[t], t.lower()))
    return terms[:MAX_TERMS]
