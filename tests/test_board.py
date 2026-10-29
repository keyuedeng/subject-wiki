import pytest

from app.classify.types import FileType as T
from app.db.models import File, Subject
from app.services.board import SlotError, build_board, group_lectures, resolve_slot


def make(name, ftype, week=None, confidence=0.85, reviewed=False, lecture=None):
    return File(
        filename=name,
        type=ftype.value,
        week=week,
        lecture=lecture,
        classify_confidence=confidence,
        reviewed=reviewed,
        included=ftype != T.READING,
    )


SUBJECT = Subject(name="ML", weeks=12)


def placed(files):
    board = build_board(SUBJECT, files)
    where = {}
    for week, slots in board.weeks.items():
        for key, fs in slots.items():
            for f in fs:
                where[f.filename] = (key, week)
    for key, fs in board.subject.items():
        for f in fs:
            where[f.filename] = (key, None)
    for f in board.readings:
        where[f.filename] = ("readings", None)
    for f, _ in board.unsorted:
        where[f.filename] = ("unsorted", None)
    return where


def test_placement():
    where = placed([
        make("slides", T.SLIDES, week=3),
        make("vtt", T.TRANSCRIPT, week=3),
        make("tute", T.TUTORIAL, week=4),
        make("tute sol", T.SOLUTIONS, week=4),
        make("2023 Final Exam Solutions.pdf", T.SOLUTIONS),
        make("Tute 4 solutions.pdf", T.SOLUTIONS),
        make("exam", T.PAST_PAPER, week=5),
        make("outline", T.OUTLINE),
        make("reading", T.READING, week=2),
        make("assignment", T.OTHER),
        make("lowconf", T.SLIDES, week=3, confidence=0.3),
        make("lowconf ok", T.SLIDES, week=3, confidence=0.3, reviewed=True),
        make("no week", T.SLIDES, lecture=4),
        make("week 14", T.SLIDES, week=14),
    ])  # fmt: skip
    assert where == {
        "slides": ("lectures", 3),
        "vtt": ("lectures", 3),
        "tute": ("tutorials", 4),
        "tute sol": ("tutorials", 4),  # solutions sit in their tutorial's group
        "2023 Final Exam Solutions.pdf": ("exams", None),
        "Tute 4 solutions.pdf": ("unsorted", None),
        "exam": ("exams", None),
        "outline": ("outline", None),
        "reading": ("readings", None),
        "assignment": ("subject_other", None),
        "lowconf": ("unsorted", None),
        "lowconf ok": ("lectures", 3),
        "no week": ("unsorted", None),
        "week 14": ("unsorted", None),
    }


def test_unsorted_reasons():
    files = [make("w14", T.SLIDES, week=14), make("low", T.OTHER, confidence=0.2)]
    board = build_board(SUBJECT, files)
    reasons = {f.filename: note for f, note in board.unsorted}
    assert reasons["w14"] == "Week 14 is outside weeks 1–12"
    assert reasons["low"].startswith("Low-confidence guess")


@pytest.mark.parametrize(
    ("slot", "week", "current", "expected"),
    [
        ("lectures", 3, T.TRANSCRIPT, (T.TRANSCRIPT, 3)),  # accepted type kept
        ("lectures", 3, T.TUTORIAL, (T.SLIDES, 3)),  # switched to the slot's default
        ("tutorials", 5, T.OTHER, (T.TUTORIAL, 5)),
        ("exams", None, T.SOLUTIONS, (T.SOLUTIONS, None)),
        ("exams", None, T.SLIDES, (T.PAST_PAPER, None)),
        ("outline", 3, T.OTHER, (T.OUTLINE, None)),  # whole-subject slots clear the week
        ("readings", 2, T.SLIDES, (T.READING, 2)),
    ],
)
def test_resolve_slot(slot, week, current, expected):
    assert resolve_slot(slot, week, current) == expected


def test_resolve_slot_errors():
    with pytest.raises(SlotError):
        resolve_slot("nope", 1, T.SLIDES)
    with pytest.raises(SlotError):
        resolve_slot("lectures", None, T.SLIDES)


def _shape(groups):
    return [(g.lecture, [f.filename for f in g.slides], [f.filename for f in g.transcripts])
            for g in groups]  # fmt: skip


def test_lectures_pair_by_number():
    groups = group_lectures([
        make("L5.pdf", T.SLIDES, week=3, lecture=5),
        make("L6.pdf", T.SLIDES, week=3, lecture=6),
        make("L5.vtt", T.TRANSCRIPT, week=3, lecture=5),
    ])  # fmt: skip
    assert _shape(groups) == [(5, ["L5.pdf"], ["L5.vtt"]), (6, ["L6.pdf"], [])]


def test_unnumbered_transcript_joins_the_only_lecture():
    groups = group_lectures(
        [make("L5.pdf", T.SLIDES, 3, lecture=5), make("rec.txt", T.TRANSCRIPT, 3)]
    )
    assert _shape(groups) == [(5, ["L5.pdf"], ["rec.txt"])]


def test_unnumbered_pair_and_ambiguous_cases():
    pair = group_lectures([make("s.pdf", T.SLIDES, 3), make("t.vtt", T.TRANSCRIPT, 3)])
    assert _shape(pair) == [(None, ["s.pdf"], ["t.vtt"])]
    two = group_lectures([
        make("L1.pdf", T.SLIDES, 3, lecture=1),
        make("L2.pdf", T.SLIDES, 3, lecture=2),
        make("t.vtt", T.TRANSCRIPT, 3),
    ])  # fmt: skip
    assert _shape(two)[-1] == (None, [], ["t.vtt"])  # can't tell which lecture: its own chip


def test_week_completeness():
    files = [
        make("L1.pdf", T.SLIDES, 1, lecture=1),
        make("L1.vtt", T.TRANSCRIPT, 1, lecture=1),
        make("T1.pdf", T.TUTORIAL, 1),
        make("L3.pdf", T.SLIDES, 2, lecture=3),
        make("L4.pdf", T.SLIDES, 2, lecture=4),
        make("L3.vtt", T.TRANSCRIPT, 2, lecture=3),
    ]
    board = build_board(SUBJECT, files)
    week1, week2, week3 = (
        board.rows[0].completeness,
        board.rows[1].completeness,
        board.rows[2].completeness,
    )
    assert week1.complete and board.complete_weeks == 1
    assert week2.slides and not week2.transcript and not week2.tutorial
    assert week2.summary == "Slides ✓ · Transcript 1 of 2 lectures · Tutorial missing"
    assert week3.empty and not week3.complete


def test_tutorials_group_with_their_solutions():
    from app.services.board import group_tutorials

    w4, w4b = make("Workshop_04.pdf", T.TUTORIAL, 4), make("Workshop_04b.pdf", T.TUTORIAL, 4)
    sol = make("Partial_Solution_04.pdf", T.SOLUTIONS, 4)
    # Shared number, or the week's only tutorial.
    (group,) = group_tutorials([w4, sol])
    assert (group.tutorials, group.solutions) == ([w4], [sol])
    t1, t2 = make("Tute 1.pdf", T.TUTORIAL, 4), make("Tute 2.pdf", T.TUTORIAL, 4)
    s2, s9 = make("Tute 2 solutions.pdf", T.SOLUTIONS, 4), make("answers.pdf", T.SOLUTIONS, 4)
    groups = group_tutorials([t1, t2, s2, s9])
    assert [(len(g.tutorials), [s.filename for s in g.solutions]) for g in groups] == [
        (1, []), (1, ["Tute 2 solutions.pdf"]), (0, ["answers.pdf"])]  # fmt: skip
    # Ambiguous number (both tutorials share "04"): the solution stays on its own.
    assert len(group_tutorials([w4, w4b, sol])[-1].tutorials) == 0


def test_missing_counts_still_to_add_and_stats():
    from app.services.board import weeks_phrase

    files = [
        make("s4", T.SLIDES, 4, lecture=4), make("t4", T.TRANSCRIPT, 4, lecture=4),
        make("tute4", T.TUTORIAL, 4), make("sol4", T.SOLUTIONS, 4),
        make("s6", T.SLIDES, 6, lecture=6), make("s7", T.SLIDES, 7, lecture=7),
        make("sol8", T.SOLUTIONS, 8),
    ]  # fmt: skip
    board = build_board(SUBJECT, files)
    rows = {r.week: r.completeness for r in board.rows}
    assert rows[4].complete and rows[4].missing == 0
    assert rows[6].missing == 2  # transcript and tutorial; missing solutions never count
    assert rows[8].missing == 3 and not rows[8].tutorial  # solutions alone aren't a tutorial
    assert rows[1].empty and rows[1].missing == 0
    assert board.still_to_add == {"slides": [8], "transcripts": [6, 7, 8],
                                  "tutorials": [6, 7, 8]}  # fmt: skip
    assert board.stats == {"weeks": 12, "weeks_with_lectures": 3, "transcripts": 1, "tutorials": 1}
    assert weeks_phrase([6, 7]) == "weeks 6 and 7"
    assert weeks_phrase([1, 2, 3, 5, 8]) == "weeks 1–3, 5 and 8"
    assert weeks_phrase([4]) == "week 4"
