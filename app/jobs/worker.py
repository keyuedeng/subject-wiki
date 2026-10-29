"""Background processing: one file at a time in a worker thread (pages/windows run
concurrently inside a file). Slides are parsed; transcripts are cleaned and aligned; each
finished file is then indexed for search (only chunks whose text changed are embedded).

State lives in Postgres (files.status), so nothing is lost if the app restarts: files
left in "parsing" are queued again on startup, and the request-hash cache makes that free
for work already done. When a deck is re-parsed, transcripts paired with it are queued
again so their alignment points at the new slide content (cached, so it costs nothing
unless the slide text changed).
"""

import logging
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify.types import FileStatus, FileType
from app.config import get_settings
from app.db.models import File
from app.llm.cache import LlmCache
from app.llm.client import LlmClient, OpenAIProvider
from app.parse.pipeline import Parser, can_parse
from app.parse.vision import OpenAIVision
from app.search.index import Indexer
from app.storage.client import get_storage
from app.transcripts.pairing import transcripts_for
from app.transcripts.pipeline import TRANSCRIPT_EXTS, TranscriptProcessor, TranscriptSettings

logger = logging.getLogger(__name__)


def can_process_transcript(file: File) -> bool:
    return file.type == FileType.TRANSCRIPT.value and file.ext in TRANSCRIPT_EXTS


def can_queue(file: File) -> bool:
    return can_parse(file) or can_process_transcript(file)


class ParseQueue:
    def __init__(
        self,
        parser: Parser,
        transcripts: TranscriptProcessor | None = None,
        executor: Executor | None = None,
        indexer: Indexer | None = None,
    ):
        self.parser = parser
        self.transcripts = transcripts
        self.indexer = indexer
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="parse")
        parser.on_parsed = self._after_parse
        if transcripts is not None:
            transcripts.parse_slides = parser.parse
            transcripts.on_processed = self._index

    def _job(self, file_type: str):
        if file_type == FileType.TRANSCRIPT.value and self.transcripts is not None:
            return self.transcripts.process
        return self.parser.parse

    def enqueue(self, db: Session, files: list[File]) -> list[File]:
        """Mark processable files as parsing and queue them. Returns the queued files."""
        queued = [
            f for f in files
            if can_queue(f) and f.status != FileStatus.PARSING.value
            and (self.transcripts is not None or not can_process_transcript(f))
        ]  # fmt: skip
        queued.sort(key=lambda f: f.type == FileType.TRANSCRIPT.value)  # decks first
        jobs = [(self._job(f.type), f.id) for f in queued]
        for f in queued:
            f.status, f.status_detail = FileStatus.PARSING.value, "Queued"
        db.commit()
        for job, file_id in jobs:
            self.executor.submit(job, file_id)
        return queued

    def resume(self) -> int:
        """Re-queue files left in "parsing" by a previous run."""
        with self.parser.session_factory() as db:
            rows = db.execute(
                select(File.id, File.type).where(File.status == FileStatus.PARSING.value)
            ).all()
        for file_id, file_type in rows:
            self.executor.submit(self._job(file_type), file_id)
        if rows:
            logger.info("resumed %d interrupted job(s)", len(rows))
        return len(rows)

    def _after_parse(self, slides_id: uuid.UUID) -> None:
        self._index(slides_id)
        self._requeue_paired_transcripts(slides_id)

    def _index(self, file_id: uuid.UUID) -> None:
        """Index a finished file and note the result after its status summary. A failure
        is reported there but doesn't fail the file: indexing again is cheap."""
        if self.indexer is None:
            return
        try:
            note = self.indexer.index_file(file_id).summary
        except Exception as exc:
            logger.exception("indexing failed for file %s", file_id)
            note = f"search index failed ({type(exc).__name__}: {exc}"[:300] + ")"
        with self.parser.session_factory() as db:
            file = db.get(File, file_id)
            if file is not None:
                file.status_detail = " · ".join(p for p in (file.status_detail, note) if p)
                db.commit()

    def _requeue_paired_transcripts(self, slides_id: uuid.UUID) -> None:
        if self.transcripts is None:
            return
        with self.parser.session_factory() as db:
            ids = transcripts_for(db, slides_id)
            files = [
                f for f in db.scalars(select(File).where(File.id.in_(ids)))
                if f.status == FileStatus.PARSED.value
            ]  # fmt: skip
            if files:
                self.enqueue(db, files)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


def _vision_factory():
    settings = get_settings()
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    return OpenAIVision(key, settings.slides_model, settings.slides_reasoning_effort)


def _provider_factory():
    settings = get_settings()
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    return OpenAIProvider(key)


@lru_cache
def get_queue() -> ParseQueue:
    """FastAPI dependency; tests override it with an inline queue."""
    from app.db.session import _session_factory

    settings = get_settings()
    session_factory = _session_factory()
    storage = get_storage()
    parser = Parser(
        session_factory=session_factory,
        storage=storage,
        vision_factory=_vision_factory,
        concurrency=settings.parse_concurrency,
    )
    llm = LlmClient(_provider_factory, LlmCache(session_factory))
    transcripts = TranscriptProcessor(
        session_factory=session_factory,
        storage=storage,
        llm=llm,
        settings=TranscriptSettings(
            clean_model=settings.transcript_clean_model,
            clean_effort=settings.transcript_clean_effort,
            embed_model=settings.embedding_model,
            embed_dimensions=settings.embedding_dimensions,
            concurrency=settings.parse_concurrency,
        ),
    )
    indexer = Indexer(session_factory, llm, settings.embedding_model, settings.embedding_dimensions)
    return ParseQueue(parser, transcripts, indexer=indexer)
