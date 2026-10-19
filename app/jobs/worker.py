"""Background parsing: one file at a time in a worker thread (pages run concurrently
inside a file). State lives in Postgres (files.status), so nothing is lost if the app
restarts: files left in "parsing" are queued again on startup, and the parse cache makes
that free for pages already done.
"""

import logging
import uuid
from concurrent.futures import Executor, ThreadPoolExecutor
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify.types import FileStatus
from app.config import get_settings
from app.db.models import File
from app.parse.pipeline import Parser, can_parse
from app.parse.vision import OpenAIVision
from app.storage.client import get_storage

logger = logging.getLogger(__name__)


class ParseQueue:
    def __init__(self, parser: Parser, executor: Executor | None = None):
        self.parser = parser
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="parse")

    def enqueue(self, db: Session, files: list[File]) -> list[File]:
        """Mark parseable files as parsing and queue them. Returns the queued files."""
        queued = [f for f in files if can_parse(f) and f.status != FileStatus.PARSING.value]
        for f in queued:
            f.status, f.status_detail = FileStatus.PARSING.value, "Queued"
        db.commit()
        for f in queued:
            self.executor.submit(self.parser.parse, f.id)
        return queued

    def resume(self) -> int:
        """Re-queue files left in "parsing" by a previous run."""
        with self.parser.session_factory() as db:
            ids = list(db.scalars(select(File.id).where(File.status == FileStatus.PARSING.value)))
        for file_id in ids:
            self.executor.submit(self.parser.parse, file_id)
        if ids:
            logger.info("resumed %d interrupted parse(s)", len(ids))
        return len(ids)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    def parse_one(self, file_id: uuid.UUID) -> None:
        self.executor.submit(self.parser.parse, file_id)


def _vision_factory():
    settings = get_settings()
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    return OpenAIVision(key, settings.slides_model, settings.slides_reasoning_effort)


@lru_cache
def get_queue() -> ParseQueue:
    """FastAPI dependency; tests override it with an inline queue."""
    from app.db.session import _session_factory

    settings = get_settings()
    parser = Parser(
        session_factory=_session_factory(),
        storage=get_storage(),
        vision_factory=_vision_factory,
        concurrency=settings.parse_concurrency,
    )
    return ParseQueue(parser)
