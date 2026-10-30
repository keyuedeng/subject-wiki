"""Plain-language labels for files on the board and in the review table: type names and
one status line per file ("44 slides · parsed", "55 min · matched to slides",
"Paired with Workshop 4"). Built from rows the page already loaded, plus transcript
durations."""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.classify.types import FileType as T
from app.db.models import File, TranscriptSegment
from app.services.board import Board

TYPE_LABELS = {
    T.SLIDES.value: "Lecture slides",
    T.TRANSCRIPT.value: "Transcript",
    T.TUTORIAL.value: "Tutorial",
    T.SOLUTIONS.value: "Solutions",
    T.PAST_PAPER.value: "Past exam",
    T.OUTLINE.value: "Outline",
    T.READING.value: "Reading",
    T.OTHER.value: "Other",
}

# Review table filters: (key, label, types). "all" and "review" are handled separately.
TABLE_FILTERS = [
    ("slides", "Slides", {T.SLIDES.value}),
    ("transcripts", "Transcripts", {T.TRANSCRIPT.value}),
    ("tutorials", "Tutorials", {T.TUTORIAL.value, T.SOLUTIONS.value}),
    ("exams", "Past exams", {T.PAST_PAPER.value}),
    ("outline", "Outline", {T.OUTLINE.value}),
    ("readings", "Readings", {T.READING.value}),
    ("other", "Other", {T.OTHER.value}),
]


@dataclass(frozen=True)
class Status:
    text: str
    tone: str = ""  # "", "busy", "warn" or "error"
    action: str | None = None  # "Parse", "Process" or "Retry": a button that queues the file
    reason: str | None = None  # why it failed, shown on hover
    suggestion: "Suggested | None" = None  # a deck to confirm, for unpaired transcripts


@dataclass(frozen=True)
class Suggested:
    """A deck suggested for an unpaired transcript by text similarity."""

    deck_id: uuid.UUID
    deck_title: str
    score: float
    runner_up: float | None

    @property
    def detail(self) -> str:
        """For the hover: how clear the choice was."""
        if self.runner_up is None:
            return f"The only deck in its week (text match {self.score:.2f})"
        return f"Best text match {self.score:.2f}; next best deck {self.runner_up:.2f}"


def failure_reason(f: File) -> str | None:
    """The stored error of a failed file, readable: its first line (database errors carry
    the SQL statement after it), at most 300 characters."""
    if f.status != "failed" or not f.status_detail:
        return None
    first = f.status_detail.strip().splitlines()[0].strip()
    return first if len(first) <= 300 else first[:299] + "…"


def durations(db: Session, file_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Transcript length in ms: the end of its last segment."""
    if not file_ids:
        return {}
    rows = db.execute(
        select(TranscriptSegment.file_id, func.max(TranscriptSegment.end_ms))
        .where(TranscriptSegment.file_id.in_(file_ids))
        .group_by(TranscriptSegment.file_id)
    ).all()
    return {fid: ms for fid, ms in rows if ms}


def tutorial_pairs(board: Board, titles: dict[uuid.UUID, str]) -> dict[uuid.UUID, str]:
    """Solutions file id -> title of the tutorial it's grouped with."""
    out = {}
    for row in board.rows:
        for group in row.tutorials:
            if group.tutorials:
                for sol in group.solutions:
                    out[sol.id] = titles[group.tutorials[0].id]
    return out


def file_status(
    f: File,
    *,
    duration_ms: int | None = None,
    paired_decks: list[str] | None = None,
    tutorial: str | None = None,
    suggestion: Suggested | None = None,
) -> Status:
    if f.type == T.SLIDES.value:
        return _processed(f, f"{f.page_count} slides · parsed" if f.page_count else "Parsed",
                          "Parse", "not parsed yet")  # fmt: skip
    if f.type == T.TRANSCRIPT.value:
        if not paired_decks and f.status != "parsing":
            if suggestion is not None:
                return Status("Pairing needed", "warn", suggestion=suggestion)
            return Status("Pairing needed: choose its deck", "warn", reason=failure_reason(f))
        if f.status == "parsed":
            length = f"{round(duration_ms / 60000)} min · " if duration_ms else ""
            return Status(f"{length}matched to slides" if f.has_timestamps
                          else f"{length}processed · no timestamps")  # fmt: skip
        return _processed(f, "", "Process", "not processed yet")
    if f.type == T.SOLUTIONS.value:
        return Status(f"Paired with {tutorial}") if tutorial else Status("No tutorial to pair with")
    if f.type == T.TUTORIAL.value:
        return Status("Uploaded · not parsed yet")
    if f.type == T.READING.value:
        return Status("Included in search" if f.included else "Left out")
    return Status("Uploaded")


def _processed(f: File, done: str, verb: str, waiting: str) -> Status:
    if f.status == "parsed":
        return Status(done)
    if f.status == "parsing":
        return Status(f.status_detail or "Working…", "busy")
    if f.status == "failed":
        return Status(
            f"{'Parse' if verb == 'Parse' else 'Processing'} failed",
            "error",
            "Retry",
            failure_reason(f),
        )
    return Status(f"Uploaded · {waiting}", "", verb)
