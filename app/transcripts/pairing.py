"""Which slides file(s) each transcript belongs to.

Auto-pairing, in order:
1. lecture number: the subject's slides files with the transcript's lecture number (and
   the same week, when both have one);
2. week: when the transcript's week has exactly one deck and one transcript, they pair
   (unless both have lecture numbers and they differ).
Anything else (several decks in the week, no number to tell them apart) gets a
suggestion by text similarity (`refresh_suggestions`, app/transcripts/suggest.py) for the
user to confirm; a suggestion is never used until confirmed.

Manual pairings, set in the UI or by confirming a suggestion, are never replaced by
auto-pairing; "reset to automatic" deletes them.
"""

import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.classify.types import FileStatus, FileType
from app.db.models import File, PairingSuggestion, Slide, TranscriptDeck
from app.transcripts.suggest import suggest_deck

logger = logging.getLogger(__name__)

# The processor's error for a transcript with no deck; cleared once one is paired.
NO_DECK_ERROR = "Pair this transcript with its slides first"


def by_lecture(transcript: File, slides_files: list[File]) -> list[File]:
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


def by_week(transcript: File, slides_files: list[File], transcripts: list[File]) -> list[File]:
    """The week's only deck, if this is the week's only transcript."""
    if transcript.week is None:
        return []
    decks = [f for f in slides_files if f.week == transcript.week]
    others = [t for t in transcripts if t.week == transcript.week]
    if len(decks) != 1 or len(others) != 1:
        return []
    deck = decks[0]
    if None not in (deck.lecture, transcript.lecture) and deck.lecture != transcript.lecture:
        return []  # numbered as different lectures: let the user decide
    return decks


def auto_candidates(
    transcript: File, slides_files: list[File], transcripts: list[File] | None = None
) -> list[File]:
    return by_lecture(transcript, slides_files) or by_week(
        transcript, slides_files, transcripts if transcripts is not None else [transcript]
    )


def pair_reason(transcript: File, deck: File, source: str) -> str:
    """How a pairing was made, for the transcript view."""
    if source == "manual":
        return "set by you"
    if transcript.lecture is not None and transcript.lecture == deck.lecture:
        return "matched by lecture number"
    return f"matched by week (the only deck and transcript in week {transcript.week})"


def sync_auto_pairs(db: Session, subject_id: uuid.UUID) -> None:
    """Refresh automatic pairings for a subject's transcripts. Commits."""
    files = list(db.scalars(select(File).where(File.subject_id == subject_id)))
    slides = [f for f in files if f.type == FileType.SLIDES.value]
    transcripts = [f for f in files if f.type == FileType.TRANSCRIPT.value]
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
    for t in transcripts:
        if t.id in manual:
            continue
        want = [f.id for f in auto_candidates(t, slides, transcripts)]
        have = [p.slides_file_id for p in sorted(current.get(t.id, []), key=lambda p: p.position)]
        if want == have:
            continue
        db.execute(delete(TranscriptDeck).where(TranscriptDeck.transcript_file_id == t.id))
        db.add_all(
            TranscriptDeck(transcript_file_id=t.id, slides_file_id=s, position=i, source="auto")
            for i, s in enumerate(want)
        )
        if want:
            clear_no_deck_error(t)
        changed = True
    if changed:
        db.commit()


def clear_no_deck_error(transcript: File) -> None:
    """A transcript that failed only for lack of a deck is ready to process once paired."""
    if transcript.status == FileStatus.FAILED.value and (transcript.status_detail or "").startswith(
        NO_DECK_ERROR
    ):
        transcript.status, transcript.status_detail = FileStatus.UPLOADED.value, None


# Suggestions


def suggestion_candidates(transcript: File, slides_files: list[File]) -> list[File]:
    """Decks a suggestion is chosen from: the transcript's week, else the whole subject."""
    if transcript.week is not None:
        return [f for f in slides_files if f.week == transcript.week]
    return list(slides_files)


def _candidate_key(decks: list[File]) -> list[str]:
    return sorted(f"{d.id}:{d.status}" for d in decks)


def refresh_suggestions(db: Session, storage, subject_id: uuid.UUID) -> None:
    """Suggest a deck for each unpaired transcript whose suggestion is missing or was
    made from a different set of decks. Reads each such transcript from storage once;
    stored results are reused until the candidates change. Commits."""
    from app.transcripts.formats import parse_transcript

    files = list(db.scalars(select(File).where(File.subject_id == subject_id)))
    slides = [f for f in files if f.type == FileType.SLIDES.value]
    paired = set(pairs_by_subject(db, subject_id))
    existing = {
        s.transcript_file_id: s
        for s in db.scalars(
            select(PairingSuggestion)
            .join(File, File.id == PairingSuggestion.transcript_file_id)
            .where(File.subject_id == subject_id)
        )
    }
    texts: dict | None = None  # deck id -> slide texts, loaded on first need
    changed = False
    for t in (f for f in files if f.type == FileType.TRANSCRIPT.value):
        old = existing.get(t.id)
        decks = suggestion_candidates(t, slides)
        if t.id in paired or not decks:
            if old is not None:
                db.delete(old)
                changed = True
            continue
        key = _candidate_key(decks)
        if old is not None and old.candidates == key:
            continue
        if texts is None:
            texts = _slide_texts(db, [d.id for d in slides])
        parsed = {d.id: texts[d.id] for d in decks if texts.get(d.id)}
        if not parsed:  # no candidate deck is parsed yet: nothing to compare with
            continue
        try:
            said = " ".join(
                c.text for c in parse_transcript(storage.get(t.storage_path), t.ext).cues
            )
        except Exception:
            logger.exception("couldn't read transcript %s for a pairing suggestion", t.id)
            continue
        best = suggest_deck(said, parsed, [x for v in texts.values() for x in v])
        if old is not None:
            db.delete(old)
            db.flush()
        if best is not None:
            db.add(PairingSuggestion(transcript_file_id=t.id, slides_file_id=best.deck,
                                     score=best.score, runner_up=best.runner_up,
                                     candidates=key))  # fmt: skip
        changed = True
    if changed:
        db.commit()


def _slide_texts(db: Session, deck_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    out: dict[uuid.UUID, list[str]] = {}
    rows = db.execute(
        select(Slide.file_id, Slide.title, Slide.content_json, Slide.notes).where(
            Slide.file_id.in_(deck_ids), Slide.duplicate_of.is_(None)
        )
    ).all()
    for file_id, title, content, notes in rows:
        body = (content or {}).get("content_markdown") or ""
        out.setdefault(file_id, []).append(" ".join(p for p in (title, body, notes) if p))
    return out


def suggestions_by_subject(
    db: Session, subject_id: uuid.UUID
) -> dict[uuid.UUID, PairingSuggestion]:
    """Current suggestions for unpaired transcripts (stale ones left out)."""
    files = {f.id: f for f in db.scalars(select(File).where(File.subject_id == subject_id))}
    slides = [f for f in files.values() if f.type == FileType.SLIDES.value]
    paired = set(pairs_by_subject(db, subject_id))
    out = {}
    for s in db.scalars(select(PairingSuggestion).where(
            PairingSuggestion.transcript_file_id.in_(list(files)))):  # fmt: skip
        t = files.get(s.transcript_file_id)
        if t is None or t.id in paired or s.slides_file_id not in files:
            continue
        if s.candidates == _candidate_key(suggestion_candidates(t, slides)):
            out[t.id] = s
    return out


def set_manual(db: Session, transcript_id: uuid.UUID, slides_ids: list[uuid.UUID]) -> None:
    """Pair a transcript with these decks, in this order. Commits."""
    db.execute(delete(TranscriptDeck).where(TranscriptDeck.transcript_file_id == transcript_id))
    if slides_ids and (t := db.get(File, transcript_id)) is not None:
        clear_no_deck_error(t)
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
