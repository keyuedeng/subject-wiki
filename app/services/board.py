"""The week board: which slot each file sits in, and what dropping into a slot means.

A slot accepts a set of types. Dropping (or moving) a file into a slot keeps its
type if the slot accepts it, otherwise switches to the slot's default type.
"""

from dataclasses import dataclass, field

from app.classify import patterns as p
from app.classify.types import FileType as T
from app.db.models import File, Subject


@dataclass(frozen=True)
class Slot:
    key: str
    label: str
    accepts: frozenset[T]
    default: T
    weekly: bool  # True: one per week; False: one for the whole subject


SLOTS = {
    s.key: s
    for s in [
        Slot("lectures", "Lectures", frozenset({T.SLIDES, T.TRANSCRIPT}), T.SLIDES, True),
        Slot("tutorials", "Tutorials", frozenset({T.TUTORIAL}), T.TUTORIAL, True),
        Slot("solutions", "Solutions", frozenset({T.SOLUTIONS}), T.SOLUTIONS, True),
        Slot("week_other", "Other", frozenset({T.OTHER}), T.OTHER, True),
        Slot("outline", "Outline", frozenset({T.OUTLINE}), T.OUTLINE, False),
        Slot(
            "exams",
            "Past papers & exam solutions",
            frozenset({T.PAST_PAPER, T.SOLUTIONS}),
            T.PAST_PAPER,
            False,
        ),
        Slot("subject_other", "Other", frozenset(T) - {T.READING}, T.OTHER, False),
        Slot(
            "readings", "Readings (excluded by default)", frozenset({T.READING}), T.READING, False
        ),
    ]
}
WEEK_SLOT_KEYS = [k for k, s in SLOTS.items() if s.weekly]
SUBJECT_SLOT_KEYS = ["outline", "exams", "subject_other"]


class SlotError(ValueError):
    pass


def resolve_slot(slot_key: str, week: int | None, current: T) -> tuple[T, int | None]:
    """The (type, week) a file gets when placed in a slot."""
    slot = SLOTS.get(slot_key)
    if slot is None:
        raise SlotError(f"Unknown slot '{slot_key}'")
    if slot.weekly and week is None:
        raise SlotError(f"Slot '{slot_key}' needs a week")
    new_type = current if current in slot.accepts else slot.default
    if slot.key == "readings":
        return new_type, week  # readings keep whatever week they had
    return new_type, week if slot.weekly else None


@dataclass
class LectureGroup:
    """One lecture in a week: its slides and transcript(s), paired by lecture number."""

    lecture: int | None
    slides: list[File] = field(default_factory=list)
    transcripts: list[File] = field(default_factory=list)

    @property
    def files(self) -> list[File]:
        return self.slides + self.transcripts

    @property
    def primary(self) -> File:
        return (self.slides or self.transcripts)[0]

    @property
    def timestamped(self) -> bool:
        return any(t.has_timestamps for t in self.transcripts)


@dataclass
class Completeness:
    slides: bool
    lectures: int
    lectures_with_transcript: int
    tutorial: bool
    empty: bool

    @property
    def transcript(self) -> bool:
        return self.lectures > 0 and self.lectures_with_transcript == self.lectures

    @property
    def complete(self) -> bool:
        return self.slides and self.transcript and self.tutorial

    @property
    def summary(self) -> str:
        if self.lectures and 0 < self.lectures_with_transcript < self.lectures:
            transcript = f"Transcript {self.lectures_with_transcript} of {self.lectures} lectures"
        else:
            transcript = "Transcript ✓" if self.transcript else "Transcript missing"
        return " · ".join([
            "Slides ✓" if self.slides else "Slides missing",
            transcript,
            "Tutorial ✓" if self.tutorial else "Tutorial missing",
        ])  # fmt: skip


@dataclass
class WeekRow:
    week: int
    lectures: list[LectureGroup]
    slots: dict[str, list[File]]  # tutorials, solutions, week_other
    completeness: Completeness


@dataclass
class Board:
    weeks: dict[int, dict[str, list[File]]]
    subject: dict[str, list[File]]
    readings: list[File]
    unsorted: list[tuple[File, str]] = field(default_factory=list)
    rows: list[WeekRow] = field(default_factory=list)

    @property
    def complete_weeks(self) -> int:
        return sum(row.completeness.complete for row in self.rows)


def build_board(subject: Subject, files: list[File]) -> Board:
    weeks = {w: {k: [] for k in WEEK_SLOT_KEYS} for w in range(1, subject.weeks + 1)}
    board = Board(weeks=weeks, subject={k: [] for k in SUBJECT_SLOT_KEYS}, readings=[])
    for f in sorted(files, key=lambda f: (f.week or 0, f.lecture or 0, f.filename.lower())):
        place = _place(f, subject)
        if place is None:
            board.unsorted.append((f, _unsorted_reason(f, subject)))
        elif place == ("readings", None):
            board.readings.append(f)
        elif place[1] is None:
            board.subject[place[0]].append(f)
        else:
            board.weeks[place[1]][place[0]].append(f)
    board.rows = [_week_row(week, slots) for week, slots in board.weeks.items()]
    return board


def group_lectures(files: list[File]) -> list[LectureGroup]:
    """Pair slides with transcripts by lecture number. A transcript without a number
    joins the week's only lecture; one unnumbered slides + transcript pair up."""
    numbered: dict[int, LectureGroup] = {}
    loose_slides: list[File] = []
    loose_transcripts: list[File] = []
    for f in files:
        is_slides = f.type == T.SLIDES.value
        if f.lecture is None:
            (loose_slides if is_slides else loose_transcripts).append(f)
            continue
        group = numbered.setdefault(f.lecture, LectureGroup(f.lecture))
        (group.slides if is_slides else group.transcripts).append(f)

    groups = [numbered[n] for n in sorted(numbered)]
    if loose_transcripts and not loose_slides and len(groups) == 1:
        groups[0].transcripts += loose_transcripts
        loose_transcripts = []
    if not groups and len(loose_slides) == 1 and len(loose_transcripts) == 1:
        return [LectureGroup(None, loose_slides, loose_transcripts)]
    groups += [LectureGroup(None, slides=[f]) for f in loose_slides]
    groups += [LectureGroup(None, transcripts=[f]) for f in loose_transcripts]
    return groups


def _week_row(week: int, slots: dict[str, list[File]]) -> WeekRow:
    lectures = group_lectures(slots["lectures"])
    completeness = Completeness(
        slides=any(g.slides for g in lectures),
        lectures=len(lectures),
        lectures_with_transcript=sum(bool(g.transcripts) for g in lectures),
        tutorial=bool(slots["tutorials"]),
        empty=not any(slots.values()),
    )
    others = {k: v for k, v in slots.items() if k != "lectures"}
    return WeekRow(week, lectures, others, completeness)


def _place(f: File, subject: Subject) -> tuple[str, int | None] | None:
    ftype = T(f.type)
    if ftype == T.READING:
        return ("readings", None)
    if f.needs_review:
        return None
    if ftype == T.OUTLINE:
        return ("outline", None)
    if ftype == T.PAST_PAPER:
        return ("exams", None)

    week = f.week
    if week is not None and not 1 <= week <= subject.weeks:
        return None
    if ftype == T.SOLUTIONS and week is None:
        return ("exams", None) if _looks_like_exam(f) else None
    if ftype == T.OTHER and week is None:
        return ("subject_other", None)
    if week is None:
        return None
    slot = {T.SLIDES: "lectures", T.TRANSCRIPT: "lectures", T.TUTORIAL: "tutorials",
            T.SOLUTIONS: "solutions"}.get(ftype, "week_other")  # fmt: skip
    return (slot, week)


def _looks_like_exam(f: File) -> bool:
    exam_re = dict(p.FILENAME_KEYWORDS)[T.PAST_PAPER]
    return bool(exam_re.search(p.normalize(p.split_ext(f.filename)[0])))


def _unsorted_reason(f: File, subject: Subject) -> str:
    if f.needs_review:
        return f"Low-confidence guess ({f.classify_confidence:.0%})"
    if f.week is not None and not 1 <= f.week <= subject.weeks:
        return f"Week {f.week} is outside weeks 1–{subject.weeks}"
    return "No week"
