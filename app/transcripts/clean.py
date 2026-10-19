"""Clean and flag one transcript window: a single structured call per window.

The request is laid out shared-prefix first (prompt, then the lecture's glossary, then
the window), so the provider's prompt cache covers the common part across windows.

Guards run on every result; a failed guard keeps the raw text instead:
- no added content: at least MIN_KEPT of the cleaned words must occur in the raw window
  or the glossary (corrections use glossary terms)
- exam hints survive: every exam_hint flag's quote must still be (mostly) present in the
  cleaned text
Flags are judged on the raw text; each quote is checked against it.
"""

import difflib
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CLEAN_PROMPT_VERSION = "clean-v1"
MAX_OUTPUT_TOKENS = 4000
MIN_KEPT = 0.85  # share of cleaned words that must come from the raw text or glossary
MIN_HINT_KEPT = 0.7  # share of an exam hint's words that must survive cleaning
MIN_CONTENT_WORDS = 8  # an empty clean of a window with this much real speech is rejected
FILLERS = {
    "um", "uh", "erm", "er", "ah", "like", "so", "ok", "okay", "yeah", "right", "you",
    "know", "i", "mean", "well", "just", "actually", "basically", "the", "a", "an", "and",
}  # fmt: skip
WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")

FlagType = Literal["exam_hint", "worked_example", "student_question", "admin"]

CLEAN_PROMPT = """\
You clean one window of an automatic lecture transcript for a study tool. The input has \
a glossary of terms from this lecture's slides, the end of the previous window (context \
only), words the speech recogniser was unsure about, and the window itself. \
"[Other speaker]" marks speech from someone other than the lecturer (usually a student).

clean_text: the same speech, with
- filler words and false starts removed (um, uh, repeated words, restarted half-sentences)
- speech-recognition errors fixed only when the intended word is clear from the glossary \
or the context, e.g. "you mel" -> "UML", "graspy" -> "GRASP", "create a pattern" -> \
"Creator pattern". Unsure words are the most likely to be wrong, but only fix them when \
you are confident.
- keep "[Other speaker]" markers where they are
Never add, summarise, reorder or explain content. Never answer questions. Keep the \
lecturer's own phrasing. When unsure, keep the original words. Keep every sentence that \
hints at what will be assessed, word for word apart from fillers.

flags: judge these on the original window text, not your cleaned version. Each flag \
gets a one-line reason and an exact quote copied from the original window.
- exam_hint: the lecturer says something is assessed or important to know ("this will \
be on the exam", "make sure you know", "a typical exam question", "you will be asked")
- worked_example: the lecturer works through an example or exercise live
- student_question: a student asks or answers a question
- admin: logistics, assignments and deadlines, audio checks, breaks, introductions, \
small talk
A window can have several flags or none.
"""


class Flag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: FlagType
    reason: str = Field(description="One line: why this flag applies.")
    quote: str = Field(description="Exact words copied from the original window.")


class CleanedWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clean_text: str
    flags: list[Flag]


def request_text(raw: str, glossary: list[str], previous: str | None, unsure: list[str]) -> str:
    return "\n\n".join(
        [
            "Glossary (terms from this lecture's slides):\n" + (", ".join(glossary) or "(none)"),
            "End of previous window (context only, do not output):\n"
            + (previous or "(start of lecture)"),
            "Words the recogniser was unsure about: " + (", ".join(unsure) or "none"),
            "Window:\n" + raw,
        ]
    )


def _words(text: str) -> list[str]:
    return WORD_RE.findall(text.lower())


def glossary_words(glossary: list[str]) -> set[str]:
    words: set[str] = set()
    for term in glossary:
        words.update(_words(term))
        # CamelCase pieces: "MonopolyGame" -> monopoly, game
        words.update(w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", term))
    return words


@dataclass
class Checked:
    clean_text: str
    status: str  # cleaned / rejected
    note: str | None
    flags: list[dict]


def check(raw: str, result: CleanedWindow, glossary: list[str]) -> Checked:
    flags = [_verify_quote(f.model_dump(), raw) for f in result.flags]
    clean = " ".join(result.clean_text.split())
    raw_words = set(_words(raw))
    allowed = raw_words | glossary_words(glossary)

    clean_words = _words(clean)
    raw_content = [w for w in _words(raw) if w not in FILLERS]
    if not clean_words and len(raw_content) >= MIN_CONTENT_WORDS:
        return Checked(raw, "rejected", "cleaned text was empty", flags)
    if clean_words:
        kept = sum(w in allowed for w in clean_words) / len(clean_words)
        if kept < MIN_KEPT:
            new = sorted({w for w in clean_words if w not in allowed})[:8]
            return Checked(raw, "rejected", f"added words not in the speech: {new}", flags)
    clean_set = set(clean_words)
    for flag in flags:
        if flag["type"] != "exam_hint":
            continue
        hint = [w for w in _words(flag["quote"]) if w not in FILLERS]
        if hint and sum(w in clean_set or w not in raw_words for w in hint) / len(hint) < (
            MIN_HINT_KEPT
        ):
            return Checked(raw, "rejected", "cleaning dropped an exam hint", flags)
    return Checked(clean, "cleaned", None, flags)


def _verify_quote(flag: dict, raw: str) -> dict:
    """Mark whether the quote really occurs in the raw window (allowing small slips)."""
    quote = " ".join(_words(flag["quote"]))
    text = " ".join(_words(raw))
    if not quote:
        flag["quote_found"] = False
    elif quote in text:
        flag["quote_found"] = True
    else:
        matcher = difflib.SequenceMatcher(None, text, quote, autojunk=False)
        block = matcher.find_longest_match(0, len(text), 0, len(quote))
        flag["quote_found"] = block.size >= 0.8 * len(quote)
    return flag
