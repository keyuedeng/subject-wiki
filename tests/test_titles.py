import pytest

from app.classify.titles import display_title, document_title, filename_title
from app.classify.types import FileType as T


@pytest.mark.parametrize(
    ("filename", "title"),
    [
        ("COMP30027_Week3_Lecture5_DecisionTrees.pdf", "Decision Trees"),
        ("03 - Naive Bayes (1).pdf", "Naive Bayes"),
        ("W07-Lecture13-Neural-Networks.pdf", "Neural Networks"),
        ("L12 Dynamic Programming.pdf", "Dynamic Programming"),
        ("Tute 4 solutions.pdf", "Solutions"),
        ("2023 Final Exam.pdf", "2023 Final Exam"),  # years are kept
        ("DecisionTrees_v2.pdf", "Decision Trees"),
        ("KNN and SVMs.pptx", "KNN and SVMs"),  # acronyms aren't split
        ("HTMLParser notes.pdf", "HTML Parser notes"),
        ("lecture-05-transcript.vtt", "Transcript"),
        ("COMP30027_Week3_Lecture5.pdf", None),  # nothing left but numbers
        ("Wk03_L05.pdf", None),
    ],
)
def test_filename_title(filename, title):
    assert filename_title(filename, "COMP30027") == title


@pytest.mark.parametrize(
    ("candidates", "title"),
    [
        (["COMP30027 Machine Learning", "Lecture 5: Decision Trees", "Dr Jane Smith"],
         "Decision Trees"),
        (["WARNING This material has been reproduced and communicated to you",
          "Machine Learning", "Week 3 — Naive Bayes"], "Naive Bayes"),
        (["The University of Melbourne", "Semester 2, 2026", "Evaluation"], "Evaluation"),
        (["COMP30027", "Lecture 5", "12/03/2026"], None),
        (["x" * 120], None),  # a paragraph, not a title
    ],
)  # fmt: skip
def test_document_title(candidates, title):
    assert document_title(candidates, "COMP30027", "Machine Learning") == title


def test_display_title_prefers_document_then_filename_then_label():
    assert display_title("Decision Trees", "Week3_L5.pdf", T.SLIDES, 3, 5) == "Decision Trees"
    assert display_title(None, "Naive Bayes.pdf", T.SLIDES, 3, 5) == "Naive Bayes"
    assert display_title(None, "Week3_L5.pdf", T.SLIDES, 3, 5) == "Lecture 5"
    assert display_title(None, "W4.pdf", T.TUTORIAL, 4, None) == "Week 4 tutorial"
    assert display_title(None, "W4.pdf", T.OTHER, 4, None) == "Untitled"


@pytest.mark.parametrize(
    ("raw", "fixed"),
    [
        ("A PPLYING G O F D ESIGN P ATTERNS – P ART 1", "APPLYING GOF DESIGN PATTERNS – PART 1"),
        ("UML State Machine", "UML State Machine"),
        ("I/O and A B testing", "I/O and A B testing"),  # not small caps: left alone
        ("PART A", "PART A"),
    ],
)
def test_fix_small_caps(raw, fixed):
    from app.classify.titles import fix_small_caps

    assert fix_small_caps(raw) == fixed
