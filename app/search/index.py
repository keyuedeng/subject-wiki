"""Keep a file's chunks in step with its parsed rows. Idempotent: chunk ids are derived
from their source, chunks whose text is unchanged keep their embedding, chunks whose
source is gone are deleted, and embeddings go through the request-hash cache, so an
unchanged file re-indexes with no API calls.

Runs after a slides file is parsed and after a transcript is processed (app/jobs/worker.py);
`scripts.reindex` backfills a whole subject.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.classify.titles import display_title
from app.classify.types import FileType
from app.db.models import Chunk, File, Slide, TranscriptSegment, chunk_id
from app.llm.client import LlmClient
from app.search.chunks import ChunkSpec, slide_chunks, transcript_chunks

logger = logging.getLogger(__name__)

INDEXED_TYPES = (FileType.SLIDES.value, FileType.TRANSCRIPT.value)


@dataclass(frozen=True)
class IndexResult:
    chunks: int
    embedded: int  # chunks whose text changed (each one cached by its text)
    removed: int
    cost_usd: float

    @property
    def summary(self) -> str:
        new = f", {self.embedded} new" if self.embedded else ""
        return f"indexed {self.chunks} chunks{new}"


@dataclass
class Indexer:
    session_factory: Callable[[], Session]
    llm: LlmClient
    embed_model: str
    embed_dimensions: int

    def index_file(self, file_id: uuid.UUID) -> IndexResult:
        with self.session_factory() as db:
            file = db.get(File, file_id)
            if file is None:
                return IndexResult(0, 0, 0, 0.0)
            subject_id = file.subject_id
            specs = chunks_for_file(db, file)
            existing = {
                c.id: c.content_hash
                for c in db.scalars(select(Chunk).where(Chunk.file_id == file_id))
            }

        model, dims = self.embed_model, self.embed_dimensions
        rows = [(chunk_id(subject_id, s.source_key), s, s.content_hash(model, dims)) for s in specs]
        changed = [(cid, s, h) for cid, s, h in rows if existing.get(cid) != h]
        vectors, cost = [], 0.0
        if changed:
            texts = [s.embed_text for _, s, _ in changed]
            vectors, cost, _ = self.llm.embed(model=model, dimensions=dims, texts=texts)

        keep = {cid for cid, _, _ in rows}
        stale = set(existing) - keep
        with self.session_factory() as db:
            if stale:
                db.execute(delete(Chunk).where(Chunk.id.in_(stale)))
            new_vectors = {cid: v for (cid, _, _), v in zip(changed, vectors, strict=True)}
            for cid, spec, h in rows:
                chunk = db.get(Chunk, cid) or Chunk(id=cid, subject_id=subject_id, file_id=file_id)
                chunk.source_type, chunk.source_key = spec.source_type, spec.source_key
                chunk.position, chunk.title, chunk.body = spec.position, spec.title, spec.body
                chunk.meta, chunk.content_hash = spec.meta, h
                if cid in new_vectors:
                    chunk.embedding = new_vectors[cid]
                db.add(chunk)
            db.commit()
        return IndexResult(len(rows), len(changed), len(stale), cost)

    def index_subject(self, subject_id: uuid.UUID) -> dict[uuid.UUID, IndexResult]:
        with self.session_factory() as db:
            ids = list(db.scalars(select(File.id).where(File.subject_id == subject_id)))
        return {file_id: self.index_file(file_id) for file_id in ids}


def chunks_for_file(db: Session, file: File) -> list[ChunkSpec]:
    """The chunks a file should have now (none for types that aren't indexed)."""
    if file.type == FileType.SLIDES.value:
        slides = list(db.scalars(select(Slide).where(Slide.file_id == file.id)))
        deck_title = display_title(
            file.title, file.filename, FileType(file.type), file.week, file.lecture,
            file.subject.code,
        )  # fmt: skip
        return slide_chunks(slides, deck_title)
    if file.type == FileType.TRANSCRIPT.value:
        segments = list(
            db.scalars(select(TranscriptSegment).where(TranscriptSegment.file_id == file.id))
        )
        slide_ids = {s.slide_id for s in segments if s.slide_id}
        slides = {s.id: s for s in db.scalars(select(Slide).where(Slide.id.in_(slide_ids)))}
        return transcript_chunks(segments, file.sha256, slides)
    return []
