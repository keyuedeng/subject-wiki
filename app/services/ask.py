"""Search and cited Q&A for one subject, shared by the JSON API and the Ask page.

- `search`: hybrid (or single-method) search, returning citable sources with their ranks.
- `ask`: answers only from the top retrieved chunks, with validated citations, or says the
  material doesn't cover it. Questions like "where is X taught" get a coverage list by
  week instead (app/qa/where.py).

Every model call goes through the request-hash cache: the same question over the same
material costs nothing the second time. Query embeddings are cached the same way.

Follow-ups: `history` holds the chat's earlier turns. Retrieval fuses a search for the
question alone with one that adds the previous question (so "how is it different from
Expert?" still finds Creator), and the answer request includes the earlier turns as
context. `on_event` receives ("where", topic) when the question is answered with a
coverage list, ("sources", handles) once retrieval is done, and ("text", piece) as an
answer's JSON streams in.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify.titles import display_title
from app.classify.types import FileType
from app.config import get_settings
from app.db.models import Chunk, File, Subject
from app.llm.cache import LlmCache
from app.llm.client import LlmClient, OpenAIProvider
from app.qa import answer as qa
from app.qa import where as wq
from app.qa.labels import SourceRef
from app.search.backend import NO_FILTERS, Filters, PgBackend, SearchBackend
from app.search.search import Hit, Mode, Searcher, SearchResult, fuse
from app.services.board import build_board, weeks_phrase
from app.transcripts.pairing import pairs_by_subject
from app.tutorials.names import sheet_names

AskMode = Literal["auto", "answer", "where"]
OnEvent = Callable[[str, object], None]
# A follow-up like "where is this taught?" names no topic: use the previous answer's.
PRONOUN_TOPICS = {"it", "this", "that", "these", "those", "they", "them", "this topic",
                  "this concept", "that topic", "that concept"}  # fmt: skip


def _noop(kind: str, data: object) -> None:
    pass


@dataclass
class AskSettings:
    model: str
    effort: str
    embed_model: str
    embed_dimensions: int
    k: int = 8  # chunks the answer is written from
    # Below this best cosine similarity the material is taken not to cover the question,
    # and no answer call is made. Set low: the model's own "not_covered" is the main check.
    min_similarity: float = 0.2
    where_candidates: int = 40  # fused results judged for a "where" question
    where_phrase: int = 20  # plus chunks containing the topic as a phrase


@dataclass
class Source:
    ref: SourceRef
    hit: Hit | None = None


@dataclass
class Answer:
    question: str
    status: qa.Status
    answer_markdown: str
    handles: dict[str, SourceRef]  # S1.. -> source, for rendering inline citations
    cited: list[SourceRef]
    sources: list[Source]  # everything retrieved, in rank order
    missing: str | None = None
    warnings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    cached: bool = False
    model_called: bool = True
    topic: str = ""
    follow_ups: list[str] = field(default_factory=list)
    kind: str = "answer"

    @property
    def weeks(self) -> list[int]:
        return sorted({r.week for r in self.cited if r.week is not None})


@dataclass
class WhereAnswer:
    question: str
    topic: str
    weeks: list[wq.WhereWeek]
    sources: list[Source]
    missing_note: str | None = None  # weeks without files (the topic may be there too)
    cost_usd: float = 0.0
    cached: bool = False
    model_called: bool = True
    summary: str = ""
    follow_ups: list[str] = field(default_factory=list)
    candidates: int = 0  # sources judged (kept when a stored answer drops them)
    introduced_in_tutorial: bool = False  # found only in tutorial sheets
    tutorial_summary: str = ""  # what the tutorial says it is, if it explains it
    kind: str = "where"

    def __post_init__(self) -> None:
        self.candidates = self.candidates or len(self.sources)

    @property
    def found(self) -> bool:
        return any(w.items for w in self.weeks)


@dataclass
class AskService:
    llm: LlmClient
    settings: AskSettings
    backend: SearchBackend = field(default_factory=PgBackend)

    @property
    def searcher(self) -> Searcher:
        return Searcher(self.backend, self._embed_query)

    def _embed_query(self, query: str) -> list[float]:
        s = self.settings
        vectors, _, _ = self.llm.embed(
            model=s.embed_model, dimensions=s.embed_dimensions, texts=[query.strip()]
        )
        return vectors[0]

    def search(
        self,
        db: Session,
        subject: Subject,
        query: str,
        filters: Filters = NO_FILTERS,
        k: int = 10,
        mode: Mode = "hybrid",
    ) -> list[Source]:
        result = self.searcher.search(db, subject.id, query, filters, k, mode)
        refs = load_refs(db, subject, [h.chunk_id for h in result.hits])
        return [Source(refs[h.chunk_id], h) for h in result.hits if h.chunk_id in refs]

    def ask(
        self,
        db: Session,
        subject: Subject,
        question: str,
        filters: Filters = NO_FILTERS,
        mode: AskMode = "auto",
        history: list[qa.Turn] | None = None,
        on_event: OnEvent | None = None,
    ) -> Answer | WhereAnswer:
        history, emit = history or [], on_event or _noop
        topic = wq.detect_where(question) if mode != "where" else None
        if mode == "where" or (mode == "auto" and topic):
            topic = topic or question.strip().rstrip("?")
            if topic.lower() in PRONOUN_TOPICS and history and history[-1].topic:
                topic = history[-1].topic
            return self._where(db, subject, question, topic, filters, emit)
        return self._answer(db, subject, question, filters, history, emit)

    def _retrieve(
        self, db: Session, subject: Subject, question: str, filters: Filters,
        history: list[qa.Turn], k: int,
    ) -> SearchResult:  # fmt: skip
        """The question alone, fused with the question plus the previous one."""
        results = [self.searcher.search(db, subject.id, question, filters, 2 * k)]
        if history:
            context = f"{history[-1].question.strip()} {question.strip()}"
            results.append(self.searcher.search(db, subject.id, context, filters, 2 * k))
        return fuse(results, k)

    # Cited answers

    def _answer(
        self, db: Session, subject: Subject, question: str, filters: Filters,
        history: list[qa.Turn], emit: OnEvent,
    ) -> Answer:  # fmt: skip
        s = self.settings
        result = self._retrieve(db, subject, question, filters, history, s.k)
        refs = load_refs(db, subject, [h.chunk_id for h in result.hits])
        sources = [Source(refs[h.chunk_id], h) for h in result.hits if h.chunk_id in refs]
        weak = result.top_similarity is None or result.top_similarity < s.min_similarity
        if not sources or weak:
            reason = (
                "nothing in the selected weeks and sources matches it"
                if not sources and filters != NO_FILTERS
                else "nothing retrieved is close enough to the question to answer from"
            )
            return Answer(
                question, "not_covered",
                f"The uploaded material doesn't seem to cover this: {reason}.",
                {}, [], sources, missing=question.strip(), model_called=False,
            )  # fmt: skip

        ids = qa.handles(len(sources))
        handles = dict(zip(ids, (src.ref for src in sources), strict=True))
        emit("sources", handles)
        call = self.llm.structured(
            kind="qa_answer",
            prompt_version=qa.QA_PROMPT_VERSION,
            model=s.model,
            instructions=qa.QA_PROMPT,
            input_text=qa.request_text(question, list(handles.items()), history),
            schema=qa.answer_schema(ids),
            effort=s.effort,
            max_output_tokens=qa.MAX_OUTPUT_TOKENS,
            on_text=lambda piece: emit("text", piece),
        )
        checked = qa.check(call.response, ids)
        follow_ups = [f.strip() for f in call.response.get("follow_ups") or [] if f.strip()]
        return Answer(
            question, checked.status, checked.answer_markdown, handles,
            [handles[h] for h in checked.cited], sources, checked.missing, checked.warnings,
            0.0 if call.cached else (call.call.cost_usd or 0.0), call.cached,
            topic=(call.response.get("topic") or "").strip(), follow_ups=follow_ups[:2],
        )  # fmt: skip

    # "Where is X taught?"

    def _where(
        self, db: Session, subject: Subject, question: str, topic: str, filters: Filters,
        emit: OnEvent,
    ) -> WhereAnswer:  # fmt: skip
        s = self.settings
        emit("where", topic)
        result = self.searcher.search(db, subject.id, topic, filters, s.where_candidates)
        phrase = self.backend.phrase_matches(db, subject.id, topic, filters, s.where_phrase)
        ids_in_order = list(dict.fromkeys([h.chunk_id for h in result.hits] + phrase))
        refs = load_refs(db, subject, ids_in_order)
        hits = {h.chunk_id: h for h in result.hits}
        sources = [Source(refs[c], hits.get(c)) for c in ids_in_order if c in refs]
        weak = result.top_similarity is None or result.top_similarity < s.min_similarity
        if not sources or (weak and not phrase):
            return WhereAnswer(question, topic, [], sources, missing_note(db, subject, topic),
                               model_called=False)  # fmt: skip

        ids = qa.handles(len(sources))
        handles = dict(zip(ids, (src.ref for src in sources), strict=True))
        emit("sources", handles)
        call = self.llm.structured(
            kind="qa_where",
            prompt_version=wq.WHERE_PROMPT_VERSION,
            model=s.model,
            instructions=wq.WHERE_PROMPT,
            input_text=wq.request_text(topic, list(handles.items())),
            schema=wq.where_schema(ids),
            effort=s.effort,
            max_output_tokens=wq.MAX_OUTPUT_TOKENS,
        )
        levels: dict[str, wq.Level] = {}
        for item in call.response.get("items") or []:
            if item.get("id") in handles:
                levels.setdefault(item["id"], item["level"])
        weeks = wq.group_by_week([(handles[h], lvl) for h, lvl in levels.items()])
        cost = 0.0 if call.cached else (call.call.cost_usd or 0.0)
        follow_ups = [f.strip() for f in call.response.get("follow_ups") or [] if f.strip()]
        return WhereAnswer(
            question, topic, weeks, sources, missing_note(db, subject, topic, bool(weeks)),
            cost, call.cached,
            summary=(call.response.get("summary") or "").strip() if weeks else "",
            follow_ups=follow_ups[:2], introduced_in_tutorial=wq.introduced_in_tutorial(weeks),
            tutorial_summary=wq.tutorial_summary(weeks, topic),
        )  # fmt: skip


def load_refs(db: Session, subject: Subject, chunk_ids: list[uuid.UUID]) -> dict:
    """Chunk id -> SourceRef, with labels built from the files' current week/lecture."""
    if not chunk_ids:
        return {}
    rows = db.execute(
        select(Chunk, File).join(File, File.id == Chunk.file_id).where(Chunk.id.in_(chunk_ids))
    ).all()
    sheets = sheet_names(
        list(db.scalars(select(File).where(
            File.subject_id == subject.id, File.type == FileType.TUTORIAL.value))),
        subject.code,
    )  # fmt: skip
    decks_per_week: dict[int, int] = {}
    for week in db.scalars(
        select(File.week).where(
            File.subject_id == subject.id, File.type == FileType.SLIDES.value,
            File.week.is_not(None),
        )
    ):  # fmt: skip
        decks_per_week[week] = decks_per_week.get(week, 0) + 1
    out = {}
    for chunk, file in rows:
        title = display_title(
            file.title, file.filename, FileType(file.type), file.week, file.lecture, subject.code
        )
        out[chunk.id] = SourceRef(
            chunk.id, chunk.source_type, file.id, title, file.week, file.lecture,
            chunk.position, chunk.title, chunk.body, chunk.meta or {},
            decks_per_week.get(file.week or -1, 0) > 1, sheets.get(file.id) or "",
        )  # fmt: skip
    return out


def missing_note(db: Session, subject: Subject, topic: str = "", found: bool = False) -> str | None:
    """Weeks with no lecture material yet, for "where" answers: the topic may be taught in
    a week that isn't uploaded. Weeks with slides but some transcripts missing are named
    separately."""
    files = list(db.scalars(select(File).where(File.subject_id == subject.id)))
    board = build_board(subject, files, pairs_by_subject(db, subject.id))
    empty = [r.week for r in board.rows if not r.completeness.lectures]
    no_transcript = [r.week for r in board.rows
                     if r.completeness.lectures and not r.completeness.transcript]  # fmt: skip
    parts = []
    if empty:
        have = "has" if len(empty) == 1 else "have"
        tail = f", so {topic} may come up there too." if found and topic else "."
        parts.append(f"{weeks_phrase(empty).capitalize()} {have} no slides or transcripts "
                     f"uploaded yet{tail}")  # fmt: skip
    if no_transcript:
        parts.append(f"Lecture transcripts are missing for {weeks_phrase(no_transcript)}.")
    return " ".join(parts) or None


@lru_cache
def get_ask_service() -> AskService:
    """FastAPI dependency; tests override it with a fake provider and backend."""
    from app.db.session import _session_factory

    settings = get_settings()
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    llm = LlmClient(lambda: OpenAIProvider(key), LlmCache(_session_factory()))
    return AskService(
        llm,
        AskSettings(
            model=settings.qa_model,
            effort=settings.qa_effort,
            embed_model=settings.embedding_model,
            embed_dimensions=settings.embedding_dimensions,
        ),
    )
