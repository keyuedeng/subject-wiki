"""Edits and deletes for stored files, shared by the JSON API and the web pages."""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.classify.types import FileType
from app.db.models import File
from app.storage.client import Storage

UNSET = object()


@dataclass
class FileEdit:
    """Fields a person can change. UNSET leaves a field alone; None clears week/lecture."""

    type: object = UNSET
    week: object = UNSET
    lecture: object = UNSET
    included: object = UNSET
    reviewed: object = UNSET


def apply_type(file: File, new_type: FileType) -> None:
    """Set the type; readings are excluded by default, everything else included."""
    if file.type != new_type.value:
        file.type = new_type.value
        file.included = new_type != FileType.READING


def apply_edit(file: File, edit: FileEdit) -> None:
    if edit.type is not UNSET:
        apply_type(file, FileType(edit.type))
    if edit.week is not UNSET:
        file.week = edit.week
    if edit.lecture is not UNSET:
        file.lecture = edit.lecture
    if edit.included is not UNSET:
        file.included = bool(edit.included)
    # Changing type/week/lecture counts as reviewing the guess.
    if any(v is not UNSET for v in (edit.type, edit.week, edit.lecture)):
        file.reviewed = True
    if edit.reviewed is not UNSET:
        file.reviewed = bool(edit.reviewed)


def delete_file(db: Session, storage: Storage, file: File) -> None:
    """Delete the row and its object together: the row delete only commits once the
    object is gone, so a storage failure leaves both in place."""
    db.delete(file)
    db.flush()
    try:
        storage.delete(file.storage_path)
    except Exception:
        db.rollback()
        raise
    db.commit()
