"""The week board: which slot each file sits in, and what dropping into a slot means.

A slot accepts a set of types. Dropping (or moving) a file into a slot keeps its
type if the slot accepts it, otherwise switches to the slot's default type.

Each week shows four columns: Lecture (decks), Lecture transcript (beside the deck it's
paired with), Tutorial (questions and solutions grouped per tutorial) and Other. Missing
solutions are never a gap: a week is complete with slides, a transcript for every lecture,
and a tutorial.
"""

import re
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
        # Drop targets only: a transcript is shown beside its deck, so it's stored in
        # "lectures"; "week" takes anything dropped on a week with nothing in it yet.
        Slot("transcripts", "Lecture transcript", frozenset({T.TRANSCRIPT}), T.TRANSCRIPT, True),
        Slot(
            "week",
            "Week",
            frozenset({T.SLIDES, T.TRANSCRIPT, T.TUTORIAL, T.SOLUTIONS, T.OTHER}),
            T.OTHER,
            True,
        ),  # fmt: skip
        Slot("tutorials", "Tutorial", frozenset({T.TUTORIAL, T.SOLUTIONS}), T.TUTORIAL, True),
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
# The slots a week's files are stored under (the other weekly slots are drop targets).
WEEK_SLOT_KEYS = ["lectures", "tutorials", "week_other"]
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
class TutorialGroup:
    """A tutorial's questions and its solutions (either may be missing)."""

    tutorials: list[File] = field(default_factory=list)
    solutions: list[File] = field(default_factory=list)

    @property
    def files(self) -> list[File]:
        return self.tutorials + self.solutions

    @property
    def primary(self) -> File:
        return (self.tutorials or self.solutions)[0]


_NUMBER_RE = re.compile(r"(?<![a-z\d])0*(\d{1,2})(?!\d)")


def _numbers(f: File) -> set[int]:
    stem = p.split_ext(f.filename)[0].lower()
    return {int(n) for n in _NUMBER_RE.findall(stem)}


def group_tutorials(files: list[File]) -> list[TutorialGroup]:
    """Pair one week's solutions with its tutorials: by a number they share in their
    filenames ("Workshop_04" and "Partial_Solution_04"), else with the week's only
    tutorial. Solutions with no tutorial get a group of their own."""
    tutorials = [f for f in files if f.type == T.TUTORIAL.value]
    groups = [TutorialGroup([t]) for t in tutorials]
    loose = []
    for sol in (f for f in files if f.type == T.SOLUTIONS.value):
        nums = _numbers(sol)
        match = [g for g in groups if nums & _numbers(g.tutorials[0])]
        if len(match) == 1:
            match[0].solutions.append(sol)
        elif len(groups) == 1:
            groups[0].solutions.append(sol)
        else:
            loose.append(sol)
    return groups + [TutorialGroup([], [s]) for s in loose]


@dataclass
class Completeness:
    slides: bool
    lectures: int
    lectures_with_transcript: int
    tutorial: bool
    empty: bool

    @property
    def missing(self) -> int:
        """Gaps in a week that has something: slides, each lecture's transcript, tutorial."""
        if self.empty:
            return 0
        transcripts = self.lectures - self.lectures_with_transcript if self.lectures else 1
        return (not self.slides) + transcripts + (not self.tutorial)

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
    slots: dict[str, list[File]]  # tutorials (incl. solutions), week_other
    completeness: Completeness
    tutorials: list[TutorialGroup] = field(default_factory=list)


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

    @property
    def still_to_add(self) -> dict[str, list[int]]:
        """Weeks with files but a gap, by what's missing (empty weeks aren't listed)."""
        gaps: dict[str, list[int]] = {"slides": [], "transcripts": [], "tutorials": []}
        for row in self.rows:
            c = row.completeness
            if c.empty:
                continue
            if not c.slides:
                gaps["slides"].append(row.week)
            if not c.transcript:
                gaps["transcripts"].append(row.week)
            if not c.tutorial:
                gaps["tutorials"].append(row.week)
        return {k: v for k, v in gaps.items() if v}

    @property
    def stats(self) -> dict[str, int]:
        lectures = [g for row in self.rows for g in row.lectures]
        return {
            "weeks": len(self.rows),
            "weeks_with_lectures": sum(row.completeness.slides for row in self.rows),
            "transcripts": sum(len(g.transcripts) for g in lectures),
            "tutorials": sum(len(t.tutorials) for row in self.rows for t in row.tutorials),
        }


def weeks_phrase(weeks: list[int]) -> str:
    """[6] -> "week 6"; [6, 7] -> "weeks 6 and 7"; [1, 2, 3, 5, 8] -> "weeks 1–3, 5 and 8"."""
    runs: list[str] = []
    for w in sorted(set(weeks)):
        if runs and int(runs[-1].split("–")[-1]) == w - 1:
            runs[-1] = runs[-1].split("–")[0] + f"–{w}"
        else:
            runs.append(str(w))
    pairs = [r.split("–") for r in runs]  # a run of two reads better as "6 and 7"
    runs = [
        x
        for r in pairs
        for x in (r if len(r) == 2 and int(r[1]) - int(r[0]) == 1 else ["–".join(r)])
    ]
    if len(runs) == 1 and "–" not in runs[0]:
        return f"week {runs[0]}"
    body = runs[0] if len(runs) == 1 else ", ".join(runs[:-1]) + f" and {runs[-1]}"
    return f"weeks {body}"


def build_board(subject: Subject, files: list[File], pairs: dict | None = None) -> Board:
    """`pairs`: transcript id -> ordered slides ids. A paired transcript is placed with
    its first deck when that deck sits in a week's lectures."""
    pairs = pairs or {}
    weeks = {w: {k: [] for k in WEEK_SLOT_KEYS} for w in range(1, subject.weeks + 1)}
    board = Board(weeks=weeks, subject={k: [] for k in SUBJECT_SLOT_KEYS}, readings=[])
    ordered = sorted(files, key=lambda f: (f.week or 0, f.lecture or 0, f.filename.lower()))
    deck_place = {f.id: _place(f, subject) for f in ordered if f.type == T.SLIDES.value}
    for f in ordered:
        place = _place(f, subject)
        if f.type == T.TRANSCRIPT.value and pairs.get(f.id):
            first = deck_place.get(pairs[f.id][0])
            if first and first[0] == "lectures":
                place = first
        if place is None:
            board.unsorted.append((f, _unsorted_reason(f, subject)))
        elif place == ("readings", None):
            board.readings.append(f)
        elif place[1] is None:
            board.subject[place[0]].append(f)
        else:
            board.weeks[place[1]][place[0]].append(f)
    board.rows = [_week_row(week, slots, pairs) for week, slots in board.weeks.items()]
    return board


def group_lectures(files: list[File], pairs: dict | None = None) -> list[LectureGroup]:
    """Pair slides with transcripts: explicit pairings (`pairs`, transcript id -> slides
    ids) first, then by lecture number. A transcript without a number joins the week's
    only lecture; one unnumbered slides + transcript pair up."""
    pairs = pairs or {}
    by_id = {f.id: f for f in files}
    paired_groups: dict = {}  # slides id -> LectureGroup
    rest: list[File] = []
    for f in files:
        decks = [d for d in pairs.get(f.id, []) if d in by_id]
        if f.type == T.TRANSCRIPT.value and decks:
            deck = by_id[decks[0]]
            group = paired_groups.setdefault(deck.id, LectureGroup(deck.lecture, slides=[deck]))
            group.transcripts.append(f)
    taken = {f.id for g in paired_groups.values() for f in g.files}
    rest = [f for f in files if f.id not in taken]

    numbered: dict[int, LectureGroup] = {}
    loose_slides: list[File] = []
    loose_transcripts: list[File] = []
    for f in rest:
        is_slides = f.type == T.SLIDES.value
        if f.lecture is None:
            (loose_slides if is_slides else loose_transcripts).append(f)
            continue
        group = numbered.setdefault(f.lecture, LectureGroup(f.lecture))
        (group.slides if is_slides else group.transcripts).append(f)

    groups = sorted(
        [*numbered.values(), *paired_groups.values()],
        key=lambda g: (g.lecture is None, g.lecture or 0),
    )
    if loose_transcripts and not loose_slides and len(groups) == 1:
        groups[0].transcripts += loose_transcripts
        loose_transcripts = []
    if not groups and len(loose_slides) == 1 and len(loose_transcripts) == 1:
        return [LectureGroup(None, loose_slides, loose_transcripts)]
    groups += [LectureGroup(None, slides=[f]) for f in loose_slides]
    groups += [LectureGroup(None, transcripts=[f]) for f in loose_transcripts]
    return groups


def _week_row(week: int, slots: dict[str, list[File]], pairs: dict | None = None) -> WeekRow:
    lectures = group_lectures(slots["lectures"], pairs)
    completeness = Completeness(
        slides=any(g.slides for g in lectures),
        lectures=len(lectures),
        lectures_with_transcript=sum(bool(g.transcripts) for g in lectures),
        tutorial=any(f.type == T.TUTORIAL.value for f in slots["tutorials"]),
        empty=not any(slots.values()),
    )
    others = {k: v for k, v in slots.items() if k != "lectures"}
    return WeekRow(week, lectures, others, completeness, group_tutorials(slots["tutorials"]))


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
            T.SOLUTIONS: "tutorials"}.get(ftype, "week_other")  # fmt: skip
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
