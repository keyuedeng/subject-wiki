import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import DbSession, subject_or_404
from app.api.schemas import SubjectCreate, SubjectOut
from app.db.models import Subject

router = APIRouter(prefix="/api/subjects", tags=["subjects"])


@router.get("", response_model=list[SubjectOut])
def list_subjects(db: DbSession) -> list[Subject]:
    return list(db.scalars(select(Subject).order_by(Subject.created_at.desc())))


@router.post("", response_model=SubjectOut, status_code=201)
def create_subject(body: SubjectCreate, db: DbSession) -> Subject:
    subject = Subject(**body.model_dump())
    db.add(subject)
    db.commit()
    return subject


@router.get("/{subject_id}", response_model=SubjectOut)
def get_subject(subject_id: uuid.UUID, db: DbSession) -> Subject:
    return subject_or_404(db, subject_id)
