"""Classifier tests on realistic filenames (and a few contents).

Each case: filename -> expected (type, week, lecture). Confidence is checked
separately where it matters (whether a row gets highlighted for review).
"""

import pytest

from app.classify.peek import Peek
from app.classify.rules import classify
from app.classify.types import LOW_CONFIDENCE
from app.classify.types import FileType as T

CODE = "COMP30027"

FILENAME_CASES = [
    # Slides
    ("COMP30027_Week3_Lecture5.pdf", T.SLIDES, 3, 5),
    ("Wk03_L05.pdf", T.SLIDES, 3, 5),
    ("Lecture 5 - Week 3 slides (annotated).pdf", T.SLIDES, 3, 5),
    ("lec01.pptx", T.SLIDES, None, 1),
    ("L12 Dynamic Programming.pdf", T.SLIDES, None, 12),
    ("W07-Lecture13-Neural-Networks.pdf", T.SLIDES, 7, 13),
    ("COMP 30027 week10 lecture19.pdf", T.SLIDES, 10, 19),
    ("Week 2 Lecture Two.pdf", T.SLIDES, 2, 2),
    ("Weeks 1-3 review.pptx", T.SLIDES, 1, None),
    # Transcripts
    ("lecture-05-transcript.vtt", T.TRANSCRIPT, None, 5),
    ("Week 4 Lecture 7.srt", T.TRANSCRIPT, 4, 7),
    ("W3 L6 captions.txt", T.TRANSCRIPT, 3, 6),
    # Tutorials
    ("W10 Tutorial.pdf", T.TUTORIAL, 10, None),
    ("Workshop 2 Questions.docx", T.TUTORIAL, None, None),
    ("Week 5 Workshop.pdf", T.TUTORIAL, 5, None),
    ("Lab3_worksheet.docx", T.TUTORIAL, None, None),
    # Solutions (win over tutorial and exam keywords)
    ("Tute 4 solutions.pdf", T.SOLUTIONS, None, None),
    ("Week 6 Tutorial Solutions.pdf", T.SOLUTIONS, 6, None),
    ("2023 Final Exam Solutions.pdf", T.SOLUTIONS, None, None),
    ("Mid-sem test 2024 answers.pdf", T.SOLUTIONS, None, None),
    ("workshop_3_soln.pdf", T.SOLUTIONS, None, None),
    # Past papers: four-digit years are never weeks
    ("2023 Final Exam.pdf", T.PAST_PAPER, None, None),
    ("COMP30027 Sample Exam 2022.pdf", T.PAST_PAPER, None, None),
    ("midsem_2021.pdf", T.PAST_PAPER, None, None),
    # Outline
    ("COMP30027 Subject Outline 2026.pdf", T.OUTLINE, None, None),
    ("Syllabus.docx", T.OUTLINE, None, None),
    # Readings
    ("Chapter 4 - Sorting.pdf", T.READING, None, None),
    ("Week 2 reading - Mitchell ch 3.pdf", T.READING, 2, None),
    # Other
    ("Assignment 1 spec.pdf", T.OTHER, None, None),
    ("IMG_2041.pdf", T.OTHER, None, None),
]


@pytest.mark.parametrize(("filename", "ftype", "week", "lecture"), FILENAME_CASES)
def test_filename(filename, ftype, week, lecture):
    guess = classify(filename, Peek(), CODE)
    assert (guess.type, guess.week, guess.lecture) == (ftype, week, lecture), guess.reason


def test_subject_code_digits_are_never_week_or_lecture():
    guess = classify("COMP30027.pdf", Peek(), None)
    assert guess.week is None and guess.lecture is None


def test_tutorial_number_is_not_used_as_week():
    guess = classify("Tute 4 solutions.pdf", Peek(), CODE)
    assert guess.week is None
    assert "tutorial 4" in guess.reason


def test_reason_explains_the_guess():
    guess = classify("COMP30027_Week3_Lecture5.pdf", Peek(), CODE)
    assert "'lecture' in filename" in guess.reason
    assert "week 3 from filename" in guess.reason


@pytest.mark.parametrize(
    ("filename", "low"),
    [
        ("COMP30027_Week3_Lecture5.pdf", False),  # clear keyword + numbers
        ("2023 Final Exam.pdf", False),
        ("lecture-05-transcript.vtt", False),
        ("IMG_2041.pdf", True),  # nothing matched
        ("Workshop 2 Questions.docx", True),  # tutorial with no week: needs one
        ("Final lecture reading.pdf", True),  # conflicting keywords
    ],
)
def test_low_confidence_is_flagged(filename, low):
    guess = classify(filename, Peek(), CODE)
    assert (guess.confidence < LOW_CONFIDENCE) is low, (guess.confidence, guess.reason)


# Timestamps


def test_caption_files_have_timestamps():
    assert classify("lecture-05-transcript.vtt", Peek(), CODE).has_timestamps
    assert classify("lecture-05.srt", Peek(), CODE).has_timestamps


def test_txt_with_timestamp_lines_has_timestamps():
    lines = ("[00:00:05] Okay let's start", "[00:01:10] So last week", "[00:02:30] Right")
    guess = classify("Lecture 3 transcript.txt", Peek(text="\n".join(lines), lines=lines), CODE)
    assert guess.type == T.TRANSCRIPT
    assert guess.has_timestamps


def test_plain_txt_transcript_has_no_timestamps():
    guess = classify("Lecture 3 transcript.txt", Peek(text=SPEECH, lines=(SPEECH,)), CODE)
    assert guess.type == T.TRANSCRIPT
    assert not guess.has_timestamps


def test_only_transcripts_report_timestamps():
    lines = ("Due 10:00 Monday", "Lab 12:30", "Exam 09:00")
    guess = classify("Syllabus.txt", Peek(text="\n".join(lines), lines=lines), CODE)
    assert guess.type == T.OUTLINE
    assert not guess.has_timestamps


# Content


# Real export from the lecture-capture tool: one unbroken paragraph, no timestamps.
SPEECH = (
    "All right, OK. OK, um, everyone can hear me well, is that correct? Conner back? Can you "
    "thumbs up? Yes, OK. All right, good afternoon everyone, and we'll come back to SWN 30006. "
    "Uh, this is our week two, lecture two, and as I mentioned, uh, this week we will continue "
    "on the requirement analysis and basically we were focusing on using a domain modeling and "
    "how can we create a system sequence diagrams, OK? "
) * 2


def test_speech_like_txt_is_a_transcript_with_spoken_week_and_lecture():
    guess = classify("SWE30006 recording.txt", Peek(text=SPEECH, lines=(SPEECH,)), "SWE30006")
    assert (guess.type, guess.week, guess.lecture) == (T.TRANSCRIPT, 2, 2)
    assert not guess.has_timestamps
    assert "week 2 from content" in guess.reason


def test_vtt_with_spoken_week_and_lecture():
    text = (
        "WEBVTT\n\n00:00:16.290 --> 00:00:18.610\n<v Speaker 0>All right, OK.\n\n"
        'NOTE CONF {"raw":[72,76,99]}\n\n00:00:23.790 --> 00:00:27.629\n'
        "<v Speaker 0>this is our week two, lecture two.\n"
    )
    guess = classify("Media1.vtt", Peek(text=text, lines=tuple(text.splitlines())), "SWE30006")
    assert (guess.type, guess.week, guess.lecture, guess.has_timestamps) == (
        T.TRANSCRIPT,
        2,
        2,
        True,
    )


def test_filename_week_wins_over_content_week():
    text = SPEECH.replace("week two", "week nine")
    guess = classify("Week 3 Lecture 5.txt", Peek(text=text, lines=(text,)), CODE)
    assert (guess.week, guess.lecture) == (3, 5)


def test_txt_with_caption_cues_is_a_timestamped_transcript():
    text = "1\n00:00:01,000 --> 00:00:04,000\nHello\n\n2\n00:00:05,000 --> 00:00:08,000\nHi\n" * 2
    guess = classify("download.txt", Peek(text=text, lines=tuple(text.splitlines())), CODE)
    assert guess.type == T.TRANSCRIPT and guess.has_timestamps


def test_landscape_pdf_without_keywords_is_slides():
    guess = classify("Neural networks.pdf", Peek(text="Neural networks", landscape=True), CODE)
    assert guess.type == T.SLIDES


def test_exam_markers_in_content():
    text = "Examination\nReading time: 15 minutes\nWriting time: 2 hours\nTotal marks: 120"
    guess = classify("COMP30027_2019.pdf", Peek(text=text, landscape=False), CODE)
    assert guess.type == T.PAST_PAPER


def test_reading_markers_in_content():
    text = "Abstract\nWe study ...\nJournal of Machine Learning Research\nReferences\ndoi:10.1/x"
    guess = classify("Smith et al 2019.pdf", Peek(text=text, landscape=False), CODE)
    assert guess.type == T.READING


def test_content_agreeing_raises_confidence():
    plain = classify("2023 Final Exam.pdf", Peek(), CODE)
    text = "Examination\nReading time: 15 minutes\nWriting time: 2 hours"
    agreed = classify("2023 Final Exam.pdf", Peek(text=text), CODE)
    assert agreed.confidence > plain.confidence


def test_unreadable_file_falls_back_to_filename():
    guess = classify("Week 3 Lecture 5.pdf", Peek(error="could not read .pdf contents"), CODE)
    assert guess.type == T.SLIDES
    assert "could not read" in guess.reason


def test_tutorial_week_written_in_the_file_wins_over_the_filename():
    # Filed by the week it's taught: Workshop 4 is run in week 5, and the sheet says so.
    text = "Workshop 4: Fundamental Design Patterns. Pre-Workshop Learning in Workshop 4 (Week 5)"
    guess = classify("Week 4 Workshop.pdf", Peek(text=text), CODE)
    assert (guess.type, guess.week) == (T.TUTORIAL, 5)
    assert "week 5 written in the file (filename says 4)" in guess.reason
    assert guess.confidence <= 0.7
    sheet = classify("Workshop_04.pdf", Peek(text=text), CODE)  # workshop number isn't a week
    assert (sheet.type, sheet.week) == (T.TUTORIAL, 5)
    # Lecture slides keep the filename's week.
    assert classify("Week 4 Lecture 7.pdf", Peek(text="Week 5 recap"), CODE).week == 4


def test_jupyter_notebooks_are_tutorials_unless_named_solutions():
    assert classify("week3_lab.ipynb", Peek(text="# Lab 3"), CODE).type == T.TUTORIAL
    assert classify("Tutorial 3 notebook.ipynb", Peek(), CODE).type == T.TUTORIAL
    assert classify("Week 3 solutions.ipynb", Peek(), CODE).type == T.SOLUTIONS
