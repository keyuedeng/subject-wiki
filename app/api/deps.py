import uuid
from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models import File, Subject
from app.db.session import get_db
from app.jobs.worker import ParseQueue, get_queue
from app.storage.client import Storage, get_storage

DbSession = Annotated[Session, Depends(get_db)]
StorageDep = Annotated[Storage, Depends(get_storage)]
QueueDep = Annotated[ParseQueue, Depends(get_queue)]


def subject_or_404(db: Session, subject_id: uuid.UUID) -> Subject:
    subject = db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(404, "Subject not found")
    return subject


def file_or_404(db: Session, file_id: uuid.UUID) -> File:
    file = db.get(File, file_id)
    if file is None:
        raise HTTPException(404, "File not found")
    return file
