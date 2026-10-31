import uuid
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Response, UploadFile
from sqlalchemy import select

from app.api.deps import DbSession, QueueDep, StorageDep, file_or_404, subject_or_404
from app.api.schemas import (
    FileOut,
    FileUpdate,
    PairingIn,
    PairingOut,
    ParseQueued,
    SegmentOut,
    SlideOut,
    UploadOut,
)
from app.config import get_settings
from app.db.models import File, Slide, TranscriptSegment
from app.jobs.worker import can_process_transcript, can_queue
from app.services.board import SlotError
from app.services.files import FileEdit, apply_edit, delete_file
from app.services.uploads import ingest_upload, rejection_reason
from app.storage.client import StorageError
from app.transcripts.pairing import decks_for, reset_to_auto, set_manual

router = APIRouter(prefix="/api", tags=["files"])


@router.get("/subjects/{subject_id}/files", response_model=list[FileOut])
def list_files(subject_id: uuid.UUID, db: DbSession) -> list[File]:
    subject_or_404(db, subject_id)
    query = select(File).where(File.subject_id == subject_id).order_by(File.created_at)
    return list(db.scalars(query))


@router.post("/subjects/{subject_id}/files", response_model=UploadOut)
def upload_file(
    subject_id: uuid.UUID,
    file: UploadFile,
    db: DbSession,
    storage: StorageDep,
    slot: Annotated[str | None, Form()] = None,
    week: Annotated[int | None, Form()] = None,
) -> UploadOut:
    """Upload one file. The browser sends one request per file so each gets its own
    progress bar and result. `slot`/`week` are set when dropped onto the week board."""
    subject = subject_or_404(db, subject_id)
    max_bytes = get_settings().max_upload_bytes
    filename = file.filename or "unnamed"
    # Reject oversize and unsupported files before reading them into memory.
    size = file.size if file.size is not None else 0
    if size > max_bytes and (
        reason := rejection_reason(filename, file.content_type, size, max_bytes)
    ):
        return UploadOut(filename=filename, outcome="rejected", message=reason)
    data = file.file.read(max_bytes + 1)
    try:
        result = ingest_upload(
            db, storage, subject, filename, file.content_type, data,
            max_bytes=max_bytes, slot=slot, slot_week=week,
        )  # fmt: skip
    except SlotError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(502, str(exc)) from exc
    return UploadOut(
        filename=filename,
        outcome=result.outcome,
        message=result.message,
        file=FileOut.model_validate(result.file) if result.file else None,
    )


@router.patch("/files/{file_id}", response_model=FileOut)
def update_file(file_id: uuid.UUID, body: FileUpdate, db: DbSession) -> File:
    file = file_or_404(db, file_id)
    sent = body.model_dump(exclude_unset=True)
    if sent.get("type", ...) is None or sent.get("included", ...) is None:
        raise HTTPException(422, "type and included cannot be null")
    apply_edit(file, FileEdit(**sent))
    db.commit()
    return file


@router.delete("/files/{file_id}", status_code=204)
def remove_file(file_id: uuid.UUID, db: DbSession, storage: StorageDep) -> Response:
    file = file_or_404(db, file_id)
    try:
        delete_file(db, storage, file)
    except StorageError as exc:
        raise HTTPException(502, str(exc)) from exc
    return Response(status_code=204)


@router.post("/files/{file_id}/parse", response_model=ParseQueued, status_code=202)
def parse_file(file_id: uuid.UUID, db: DbSession, queue: QueueDep) -> ParseQueued:
    """Queue a slides file for parsing. Unchanged pages reuse cached results (free)."""
    file = file_or_404(db, file_id)
    if not can_queue(file):
        raise HTTPException(
            400, "Only slides, transcripts, and tutorial or solutions sheets can be processed"
        )
    if file.status == "parsing":
        raise HTTPException(409, "Already parsing")
    queued = queue.enqueue(db, [file])
    return ParseQueued(queued=len(queued), file_ids=[f.id for f in queued])


@router.post("/subjects/{subject_id}/parse", response_model=ParseQueued, status_code=202)
def parse_subject(subject_id: uuid.UUID, db: DbSession, queue: QueueDep) -> ParseQueued:
    """Queue every slides file that isn't parsed yet (or failed)."""
    subject_or_404(db, subject_id)
    files = db.scalars(
        select(File).where(File.subject_id == subject_id, File.status.in_(["uploaded", "failed"]))
    ).all()
    queued = queue.enqueue(db, list(files))
    return ParseQueued(queued=len(queued), file_ids=[f.id for f in queued])


@router.get("/files/{file_id}/slides", response_model=list[SlideOut])
def list_slides(file_id: uuid.UUID, db: DbSession) -> list[Slide]:
    return file_or_404(db, file_id).slides


@router.get("/files/{file_id}/segments", response_model=list[SegmentOut])
def list_segments(file_id: uuid.UUID, db: DbSession) -> list[TranscriptSegment]:
    file_or_404(db, file_id)
    return list(
        db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.file_id == file_id)
            .order_by(TranscriptSegment.idx)
        )
    )


@router.get("/files/{file_id}/pairing", response_model=PairingOut)
def get_pairing(file_id: uuid.UUID, db: DbSession) -> PairingOut:
    file = file_or_404(db, file_id)
    decks = decks_for(db, file.id)
    return PairingOut(
        slides_file_ids=[f.id for f, _ in decks],
        source=decks[0][1] if decks else None,
    )


@router.put("/files/{file_id}/pairing", response_model=PairingOut)
def put_pairing(file_id: uuid.UUID, body: PairingIn, db: DbSession) -> PairingOut:
    """Pair a transcript with these decks, in order (a manual pairing)."""
    file = file_or_404(db, file_id)
    if not can_process_transcript(file):
        raise HTTPException(400, "Only transcripts can be paired")
    for deck_id in body.slides_file_ids:
        deck = db.get(File, deck_id)
        if deck is None or deck.subject_id != file.subject_id or deck.type != "slides":
            raise HTTPException(400, f"{deck_id} is not a slides file in this subject")
    set_manual(db, file.id, body.slides_file_ids)
    return get_pairing(file_id, db)


@router.delete("/files/{file_id}/pairing", response_model=PairingOut)
def reset_pairing(file_id: uuid.UUID, db: DbSession) -> PairingOut:
    """Go back to automatic pairing by week and lecture number."""
    file = file_or_404(db, file_id)
    reset_to_auto(db, file)
    return get_pairing(file_id, db)
