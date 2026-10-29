import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import DbSession, subject_or_404
from app.api.schemas import (
    AnswerOut,
    AskIn,
    SearchIn,
    SourceOut,
    WhereItemOut,
    WhereOut,
    WhereWeekOut,
)
from app.search.backend import Filters
from app.services.ask import AskService, Source, get_ask_service

router = APIRouter(prefix="/api", tags=["ask"])
AskDep = Annotated[AskService, Depends(get_ask_service)]


@router.post("/subjects/{subject_id}/search", response_model=list[SourceOut])
def search(subject_id: uuid.UUID, body: SearchIn, db: DbSession, service: AskDep):
    subject = subject_or_404(db, subject_id)
    filters = Filters(tuple(body.weeks), tuple(body.types))
    return [_source(s) for s in service.search(db, subject, body.query, filters, body.k, body.mode)]


@router.post("/subjects/{subject_id}/ask", response_model=AnswerOut | WhereOut)
def ask(subject_id: uuid.UUID, body: AskIn, db: DbSession, service: AskDep):
    subject = subject_or_404(db, subject_id)
    filters = Filters(tuple(body.weeks), tuple(body.types))
    r = service.ask(db, subject, body.question, filters, body.mode)
    sources = [_source(s) for s in r.sources]
    if r.kind == "where":
        weeks = [
            WhereWeekOut(week=w.week, items=[
                WhereItemOut(level=i.level, label=i.label, href=i.href,
                             chunk_ids=[ref.chunk_id for ref in i.refs])
                for i in w.items
            ])
            for w in r.weeks
        ]  # fmt: skip
        return WhereOut(topic=r.topic, found=r.found, weeks=weeks, missing_note=r.missing_note,
                        sources=sources, cost_usd=r.cost_usd, cached=r.cached)  # fmt: skip
    return AnswerOut(
        status=r.status, answer_markdown=r.answer_markdown,
        handles={h: ref.chunk_id for h, ref in r.handles.items()},
        cited_chunk_ids=[ref.chunk_id for ref in r.cited], missing=r.missing,
        warnings=r.warnings, sources=sources, cost_usd=r.cost_usd, cached=r.cached,
    )  # fmt: skip


def _source(s: Source) -> SourceOut:
    ref, hit = s.ref, s.hit
    return SourceOut(
        chunk_id=ref.chunk_id, source_type=ref.source_type, label=ref.label, href=ref.href,
        file_id=ref.file_id, file_title=ref.file_title, week=ref.week, title=ref.title,
        body=ref.body, rank=hit.rank if hit else None,
        keyword_rank=hit.keyword_rank if hit else None,
        embedding_rank=hit.embedding_rank if hit else None,
        similarity=hit.similarity if hit else None,
    )  # fmt: skip
