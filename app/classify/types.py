"""Shared vocabulary for file classification, used by the classifier, models and UI."""

from dataclasses import dataclass
from enum import StrEnum


class FileType(StrEnum):
    SLIDES = "slides"
    TRANSCRIPT = "transcript"
    TUTORIAL = "tutorial"
    SOLUTIONS = "solutions"
    PAST_PAPER = "past_paper"
    OUTLINE = "outline"
    READING = "reading"
    OTHER = "other"


class FileStatus(StrEnum):
    UPLOADED = "uploaded"
    PARSED = "parsed"
    FAILED = "failed"


# Guesses below this are highlighted for review until the user confirms or edits them.
LOW_CONFIDENCE = 0.6


@dataclass(frozen=True)
class Guess:
    type: FileType
    week: int | None
    lecture: int | None
    has_timestamps: bool
    confidence: float
    reason: str
