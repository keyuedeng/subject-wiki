"""Shared loading for the transcript scripts: segments, paired decks' slides, and
the similarity matrix (from cached embeddings: no API calls on unchanged inputs)."""

import uuid
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select

from app.db.models import File, Slide, TranscriptSegment
from app.db.session import _session_factory
from app.jobs.worker import get_queue
from app.transcripts.align import cosine_matrix
from app.transcripts.pairing import decks_for
from app.transcripts.pipeline import slide_embedding_text


@dataclass
class Loaded:
    transcript: File
    decks: list[File]
    segments: list[TranscriptSegment]
    slides: list[Slide]  # every page of the paired decks, in order
    kept: list[Slide]  # alignment targets (not collapsed), in order
    subject_code: str | None = None

    def kept_index_of_page(self, deck_no: int, page_no: int) -> int | None:
        """Kept-slide index for a labelled page (collapsed pages count as their keeper)."""
        deck = self.decks[deck_no - 1]
        slide = next((s for s in self.slides if s.file_id == deck.id and s.page_no == page_no),
                     None)  # fmt: skip
        if slide is None:
            raise ValueError(f"deck {deck_no} has no page {page_no}")
        target = slide.duplicate_of or slide.id
        return next(i for i, k in enumerate(self.kept) if k.id == target)

    def label_of_kept(self, k: int | None) -> str:
        if k is None:
            return "-"
        s = self.kept[k]
        deck_no = next(i for i, d in enumerate(self.decks, 1) if d.id == s.file_id)
        return f"{deck_no}:{s.page_no}" if len(self.decks) > 1 else str(s.page_no)


def find_transcript(db, ref: str) -> File:
    try:
        file = db.get(File, uuid.UUID(ref))
    except ValueError:
        file = db.scalar(select(File).where(File.filename.contains(ref), File.type == "transcript"))
    if file is None:
        raise SystemExit(f"No transcript matches {ref!r}")
    return file


def load(ref: str) -> Loaded:
    with _session_factory()() as db:
        transcript = find_transcript(db, ref)
        subject_code = transcript.subject.code
        decks = [f for f, _ in decks_for(db, transcript.id)]
        if not decks:
            raise SystemExit("The transcript isn't paired with any slides yet")
        segments = list(
            db.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.file_id == transcript.id)
                .order_by(TranscriptSegment.idx)
            )
        )
        order = {d.id: i for i, d in enumerate(decks)}
        slides = sorted(db.scalars(select(Slide).where(Slide.file_id.in_(order))).all(),
                        key=lambda s: (order[s.file_id], s.page_no))  # fmt: skip
        db.expunge_all()
    if not segments:
        raise SystemExit("Process the transcript first")
    kept = [s for s in slides if s.duplicate_of is None]
    return Loaded(transcript, decks, segments, slides, kept, subject_code)


def similarity(data: Loaded) -> np.ndarray:
    """The same embeddings the pipeline used, from the cache."""
    queue = get_queue()
    settings = queue.transcripts.settings
    texts = [s.clean_text for s in data.segments]
    slide_texts = [slide_embedding_text(s) for s in data.kept]
    vectors, cost, calls = queue.transcripts.llm.embed(
        model=settings.embed_model, dimensions=settings.embed_dimensions,
        texts=texts + slide_texts,
    )  # fmt: skip
    if calls:
        print(f"(embedded {calls} new texts, ${cost:.4f})")
    return cosine_matrix(np.array(vectors[: len(texts)]), np.array(vectors[len(texts) :]))
