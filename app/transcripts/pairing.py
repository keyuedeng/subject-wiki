"""Which slides file(s) each transcript belongs to.

Auto-pairing matches a transcript with the subject's slides files that have the same
lecture number (and the same week, when both have one). Manual pairings, set in the UI,
are never replaced by auto-pairing; "reset to automatic" deletes them.
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.classify.types import FileType
from app.db.models import File, TranscriptDeck


def auto_candidates(transcript: File, slides_files: list[File]) -> list[File]:
    if transcript.lecture is None:
        return []
    out = []
    for f in slides_files:
        if f.lecture != transcript.lecture:
            continue
        if transcript.week is not None and f.week is not None and f.week != transcript.week:
            continue
        out.append(f)
    return sorted(out, key=lambda f: (f.week or 0, f.filename.lower()))


def sync_auto_pairs(db: Session, subject_id: uuid.UUID) -> None:
    """Refresh automatic pairings for a subject's transcripts. Commits."""
    files = list(db.scalars(select(File).where(File.subject_id == subject_id)))
    slides = [f for f in files if f.type == FileType.SLIDES.value]
    pairs = list(
        db.scalars(
            select(TranscriptDeck)
            .join(File, File.id == TranscriptDeck.transcript_file_id)
            .where(File.subject_id == subject_id)
        )
    )
    manual = {p.transcript_file_id for p in pairs if p.source == "manual"}
    current = {}
    for p in pairs:
        current.setdefault(p.transcript_file_id, []).append(p)
    changed = False
    for t in (f for f in files if f.type == FileType.TRANSCRIPT.value):
        if t.id in manual:
            continue
        want = [f.id for f in auto_candidates(t, slides)]
        have = [p.slides_file_id for p in sorted(current.get(t.id, []), key=lambda p: p.position)]
        if want == have:
            continue
        db.execute(delete(TranscriptDeck).where(TranscriptDeck.transcript_file_id == t.id))
        db.add_all(
            TranscriptDeck(transcript_file_id=t.id, slides_file_id=s, position=i, source="auto")
            for i, s in enumerate(want)
        )
        changed = True
    if changed:
        db.commit()


def set_manual(db: Session, transcript_id: uuid.UUID, slides_ids: list[uuid.UUID]) -> None:
    """Pair a transcript with these decks, in this order. Commits."""
    db.execute(delete(TranscriptDeck).where(TranscriptDeck.transcript_file_id == transcript_id))
    db.add_all(
        TranscriptDeck(transcript_file_id=transcript_id, slides_file_id=s, position=i,
                       source="manual")
        for i, s in enumerate(dict.fromkeys(slides_ids))
    )  # fmt: skip
    db.commit()


def reset_to_auto(db: Session, transcript: File) -> None:
    db.execute(delete(TranscriptDeck).where(TranscriptDeck.transcript_file_id == transcript.id))
    db.commit()
    sync_auto_pairs(db, transcript.subject_id)


def decks_for(db: Session, transcript_id: uuid.UUID) -> list[tuple[File, str]]:
    """(slides file, source) in order."""
    rows = db.execute(
        select(File, TranscriptDeck.source)
        .join(TranscriptDeck, TranscriptDeck.slides_file_id == File.id)
        .where(TranscriptDeck.transcript_file_id == transcript_id)
        .order_by(TranscriptDeck.position)
    ).all()
    return [(f, source) for f, source in rows]


def transcripts_for(db: Session, slides_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(TranscriptDeck.transcript_file_id).where(
                TranscriptDeck.slides_file_id == slides_id
            )
        )
    )


def pairs_by_subject(db: Session, subject_id: uuid.UUID) -> dict[uuid.UUID, list[uuid.UUID]]:
    """transcript id -> ordered slides ids, for a whole subject (board rendering)."""
    rows = db.execute(
        select(TranscriptDeck)
        .join(File, File.id == TranscriptDeck.transcript_file_id)
        .where(File.subject_id == subject_id)
        .order_by(TranscriptDeck.position)
    ).scalars()
    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for p in rows:
        out.setdefault(p.transcript_file_id, []).append(p.slides_file_id)
    return out
