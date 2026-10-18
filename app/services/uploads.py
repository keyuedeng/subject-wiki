"""Upload pipeline: validate -> hash -> dedupe -> classify -> store object -> insert row.

Order matters for consistency between Postgres and Storage:
- the duplicate check runs before anything is stored, so a re-upload is a true no-op;
- the object is written before the row, and removed again if the insert fails
  (unless another row already points at the same content-addressed path).
"""

import hashlib
import logging
import mimetypes
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.classify import patterns
from app.classify.peek import peek
from app.classify.rules import classify
from app.classify.titles import document_title
from app.classify.types import FileType
from app.db.models import File, Subject
from app.services.board import resolve_slot
from app.services.files import apply_type
from app.storage.client import Storage, object_path

logger = logging.getLogger(__name__)

Outcome = Literal["stored", "duplicate", "rejected"]

EXT_MIME = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "vtt": "text/vtt",
    "srt": "application/x-subrip",
    "txt": "text/plain",
}


@dataclass
class UploadResult:
    outcome: Outcome
    message: str
    file: File | None = None


def rejection_reason(filename: str, mime: str | None, size: int, max_bytes: int) -> str | None:
    ext = patterns.split_ext(filename)[1]
    mime = (mime or "").lower()
    if ext in patterns.AUDIO_VIDEO_EXTENSIONS or mime.startswith(("audio/", "video/")):
        return (
            "Audio and video aren't supported yet. Upload the lecture's caption file "
            "(.vtt) or transcript (.txt) instead."
        )
    if ext not in patterns.ACCEPTED_EXTENSIONS:
        shown = f".{ext}" if ext else "files without an extension"
        return f"Unsupported file type ({shown}). Accepted: PDF, PPTX, DOCX, VTT, SRT, TXT."
    if size == 0:
        return "File is empty."
    if size > max_bytes:
        return f"File is {size / 1_048_576:.0f} MB; the limit is {max_bytes / 1_048_576:.0f} MB."
    return None


def ingest_upload(
    db: Session,
    storage: Storage,
    subject: Subject,
    filename: str,
    mime: str | None,
    data: bytes,
    *,
    max_bytes: int,
    slot: str | None = None,
    slot_week: int | None = None,
) -> UploadResult:
    """Store one uploaded file in a subject. `slot`/`slot_week` come from a board drop."""
    if reason := rejection_reason(filename, mime, len(data), max_bytes):
        return UploadResult("rejected", reason)

    sha256 = hashlib.sha256(data).hexdigest()
    existing = db.scalar(select(File).where(File.subject_id == subject.id, File.sha256 == sha256))
    if existing:
        return _duplicate(existing)

    ext = patterns.split_ext(filename)[1]
    contents = peek(data, ext)
    guess = classify(filename, contents, subject.code)
    file = File(
        subject_id=subject.id,
        filename=filename,
        title=document_title(contents.title_candidates, subject.code, subject.name),
        ext=ext,
        storage_path=object_path(subject.id, sha256, ext),
        sha256=sha256,
        mime=EXT_MIME.get(ext) or mime or mimetypes.guess_type(filename)[0] or "",
        size_bytes=len(data),
        week=guess.week,
        lecture=guess.lecture,
        has_timestamps=guess.has_timestamps,
        classify_confidence=guess.confidence,
        classify_reason=guess.reason,
        reviewed=False,
    )
    apply_type(file, guess.type)
    if slot:
        new_type, file.week = resolve_slot(slot, slot_week, guess.type)
        apply_type(file, new_type)
        file.reviewed = True

    storage.put(file.storage_path, data, file.mime)
    db.add(file)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a concurrent upload of the same file: that row owns the object.
        db.rollback()
        existing = db.scalar(
            select(File).where(File.subject_id == subject.id, File.sha256 == sha256)
        )
        if existing:
            return _duplicate(existing)
        _remove_orphan(storage, file.storage_path)
        raise
    except Exception:
        db.rollback()
        _remove_orphan(storage, file.storage_path)
        raise

    return UploadResult("stored", _stored_message(file), file)


def _duplicate(existing: File) -> UploadResult:
    return UploadResult(
        "duplicate", f"Already uploaded as “{existing.filename}”; nothing changed.", existing
    )


def _stored_message(file: File) -> str:
    parts = [FileType(file.type).value.replace("_", " ")]
    if file.week is not None:
        parts.append(f"week {file.week}")
    if file.lecture is not None:
        parts.append(f"lecture {file.lecture}")
    return "Stored as " + ", ".join(parts)


def _remove_orphan(storage: Storage, path: str) -> None:
    try:
        storage.delete(path)
    except Exception:
        logger.exception("could not remove orphaned object %s", path)
