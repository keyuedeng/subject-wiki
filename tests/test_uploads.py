import hashlib

import pytest
from sqlalchemy import func, select

from app.classify.types import FileType
from app.db.models import File, Subject
from app.services.files import FileEdit, apply_edit, delete_file
from app.services.uploads import ingest_upload
from app.storage.client import StorageError

MAX = 50 * 1024 * 1024


def upload(db, storage, subject, name, data=b"hello", mime="application/pdf", **kw):
    return ingest_upload(db, storage, subject, name, mime, data, max_bytes=MAX, **kw)


def count(db) -> int:
    return db.scalar(select(func.count()).select_from(File))


def test_upload_stores_object_and_row(db, storage, subject):
    result = upload(db, storage, subject, "COMP30027_Week3_Lecture5.pdf", b"slides")
    assert result.outcome == "stored"
    f = result.file
    sha = hashlib.sha256(b"slides").hexdigest()
    assert f.sha256 == sha
    assert f.storage_path == f"{subject.id}/{sha}.pdf"
    assert storage.objects[f.storage_path] == b"slides"
    assert (f.type, f.week, f.lecture) == ("slides", 3, 5)
    assert f.status == "uploaded" and f.included and not f.reviewed


def test_reupload_is_a_no_op(db, storage, subject):
    upload(db, storage, subject, "Week 3 Lecture 5.pdf", b"same bytes")
    puts = storage.puts
    again = upload(db, storage, subject, "renamed copy.pdf", b"same bytes")
    assert again.outcome == "duplicate"
    assert "Week 3 Lecture 5.pdf" in again.message
    assert count(db) == 1
    assert storage.puts == puts  # nothing written to storage either


def test_reupload_keeps_manual_fixes(db, storage, subject):
    f = upload(db, storage, subject, "random.pdf", b"x").file
    apply_edit(f, FileEdit(type="tutorial", week=4))
    db.commit()
    upload(db, storage, subject, "random.pdf", b"x")
    db.refresh(f)
    assert (f.type, f.week, f.reviewed) == ("tutorial", 4, True)


def test_same_file_in_another_subject_is_stored_separately(db, storage, subject):
    other = Subject(name="Algorithms", code="COMP90038", weeks=12)
    db.add(other)
    db.commit()
    upload(db, storage, subject, "Academic integrity.pdf", b"shared")
    result = upload(db, storage, other, "Academic integrity.pdf", b"shared")
    assert result.outcome == "stored"
    assert count(db) == 2
    assert len(storage.objects) == 2


@pytest.mark.parametrize(
    ("name", "mime", "message"),
    [
        ("Lecture 5.mp4", "video/mp4", "Audio and video aren't supported yet"),
        ("Lecture 5.m4a", "audio/mp4", "Audio and video aren't supported yet"),
        ("recording", "audio/mpeg", "Audio and video aren't supported yet"),
        ("notes.key", "application/octet-stream", "Unsupported file type (.key)"),
        ("Makefile", "", "Unsupported file type"),
    ],
)
def test_rejected_types(db, storage, subject, name, mime, message):
    result = upload(db, storage, subject, name, b"data", mime=mime)
    assert result.outcome == "rejected"
    assert message in result.message
    assert count(db) == 0 and not storage.objects


def test_empty_and_oversize_rejected(db, storage, subject):
    assert upload(db, storage, subject, "a.pdf", b"").outcome == "rejected"
    big = ingest_upload(db, storage, subject, "a.pdf", "application/pdf", b"x" * 11, max_bytes=10)
    assert big.outcome == "rejected" and "limit" in big.message


def test_readings_are_excluded_by_default(db, storage, subject):
    f = upload(db, storage, subject, "Chapter 4 - Sorting.pdf").file
    assert f.type == "reading" and not f.included
    apply_edit(f, FileEdit(type="slides"))
    assert f.included


def test_drop_onto_board_slot_overrides_guess(db, storage, subject):
    f = upload(db, storage, subject, "random.pdf", slot="tutorials", slot_week=4).file
    assert (f.type, f.week, f.reviewed) == ("tutorial", 4, True)


def test_drop_transcript_onto_lectures_keeps_transcript_type(db, storage, subject):
    f = upload(db, storage, subject, "Media1.vtt", b"WEBVTT\n", mime="text/vtt",
               slot="lectures", slot_week=2).file  # fmt: skip
    assert (f.type, f.week, f.has_timestamps) == ("transcript", 2, True)


def test_failed_insert_removes_object(db, storage, subject, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "commit", boom)
    with pytest.raises(RuntimeError):
        upload(db, storage, subject, "Week 1 Lecture 1.pdf", b"data")
    assert not storage.objects


def test_delete_removes_row_and_object(db, storage, subject):
    f = upload(db, storage, subject, "Week 1 Lecture 1.pdf", b"data").file
    delete_file(db, storage, f)
    assert count(db) == 0 and not storage.objects


def test_delete_keeps_row_if_storage_fails(db, storage, subject):
    f = upload(db, storage, subject, "Week 1 Lecture 1.pdf", b"data").file
    storage.fail_delete = True
    with pytest.raises(StorageError):
        delete_file(db, storage, f)
    assert count(db) == 1 and storage.objects


def test_edit_marks_reviewed_and_clears_highlight(db, storage, subject):
    f = upload(db, storage, subject, "IMG_2041.pdf").file
    assert f.needs_review
    apply_edit(f, FileEdit(week=None))
    assert f.reviewed and not f.needs_review
    assert f.type == FileType.OTHER


def test_upload_stores_document_title(db, storage, subject):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=960, height=540)
    page.insert_text((50, 60), "COMP30027 Machine Learning", fontsize=14)
    page.insert_text((50, 200), "Lecture 5: Decision Trees", fontsize=36)
    f = upload(db, storage, subject, "Week3_L5.pdf", doc.tobytes()).file
    assert f.title == "Decision Trees"


def test_solutions_take_their_tutorials_week(db, storage, subject):
    from tests.slide_fixtures import make_deck

    sheet = make_deck([{"title": "Workshop 4", "lines": ["Pre-Workshop Learning (Week 5)"]}])
    tute = upload(db, storage, subject, "Workshop_04.pdf", sheet).file
    assert (tute.type, tute.week) == ("tutorial", 5)
    sol = upload(db, storage, subject, "Partial_Solution_04.pdf", b"answers").file
    assert (sol.type, sol.week) == ("solutions", 5)
    assert "week 5 from tutorial Workshop_04.pdf" in sol.classify_reason
    # Uploaded the other way round: the tutorial gives its week to the waiting solutions.
    early = upload(db, storage, subject, "Partial_Solution_06.pdf", b"answers 6").file
    assert early.week is None
    upload(db, storage, subject, "Workshop_06.pdf",
           make_deck([{"title": "Workshop 6", "lines": ["Week 7 module"]}]))  # fmt: skip
    db.refresh(early)
    assert early.week == 7


def test_ipynb_is_accepted(db, storage, subject):
    nb = b'{"cells": [{"cell_type": "markdown", "source": ["# Lab 3: Hashing"]}], "nbformat": 4}'
    f = upload(db, storage, subject, "week3_lab.ipynb", nb, mime="application/json").file
    assert (f.type, f.ext, f.title) == ("tutorial", "ipynb", "Hashing")  # numbers are stripped
