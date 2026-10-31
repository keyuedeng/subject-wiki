"""The Ask page: chats with cited answers and "where is it taught" coverage lists.

A question is posted to /ask/stream, which answers with newline-delimited JSON events:
  start   {chat_id, url, sidebar?}  the chat (created by a first question)
  status  {text}                    progress ("Reading 8 sources…")
  delta   {html}                    the answer so far, with citation chips
  done    {html, sidebar}           the finished message
  error   {html}
The answer is produced and saved by a worker thread with its own database session, so
it's kept even if the browser leaves mid-stream. Citation chips open the source preview
panel (slide image, what was said) and link to the slide viewer or transcript.
"""

import json
import logging
import queue
import re
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from markupsafe import Markup
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import DbSession, subject_or_404
from app.db.models import Chat, Chunk, File, Slide, Subject, TranscriptSegment, TutorialSection
from app.db.session import get_session_factory
from app.llm.client import ProviderError
from app.qa.answer import CITE_RE
from app.qa.labels import SourceRef
from app.qa.stream import partial_string, trim_partial_marker
from app.search.backend import Filters
from app.services import chat as chats
from app.services.ask import Answer, AskService, WhereAnswer, get_ask_service
from app.services.board import weeks_phrase
from app.tutorials.names import sheet_names
from app.web.routes import FormLists, _file_title, _render_markdown, templates

logger = logging.getLogger(__name__)
router = APIRouter(include_in_schema=False)
AskDep = Annotated[AskService, Depends(get_ask_service)]
SessionsDep = Annotated[sessionmaker, Depends(get_session_factory)]

SOURCE_TYPES = {
    "slide": "Slides", "notes": "Lecturer notes", "transcript": "Lecture transcripts",
    "tutorial": "Tutorials",
}  # fmt: skip
DELTA_INTERVAL = 0.08  # seconds between streamed snapshots of the answer
ICON_DIR = Path(__file__).parent / "templates" / "icons"
ICONS = {
    kind: Markup((ICON_DIR / f"{kind}.svg").read_text().strip())
    for kind in ("slide", "notes", "transcript", "tutorial")
}


# Citation chips


def chip(ref: SourceRef, subject_id: uuid.UUID, label: str | None = None) -> Markup:
    """A link to a source that opens it in the preview panel (and works as a plain link)."""
    return Markup(
        '<a class="cite cite-{t}" href="{href}" data-preview="/subjects/{sid}/ask/source/{cid}" '
        'title="{kind}: {file}">{icon}{label}</a>'
    ).format(
        t=ref.source_type, href=ref.href, sid=subject_id, cid=ref.chunk_id, kind=ref.kind,
        file=ref.file_title, icon=ICONS.get(ref.source_type, ""), label=label or ref.label,
    )  # fmt: skip


def cite_html(markdown: str, handles: dict[str, SourceRef], subject_id: uuid.UUID) -> Markup:
    """Render answer markdown (raw HTML disabled), then turn [S1] markers into chips.
    Markers survive markdown rendering as plain text; unknown ones are dropped."""
    html = _render_markdown(markdown)

    def replace(match: re.Match) -> str:
        refs = [handles.get(h) for h in re.split(r"\s*[,;]\s*", match.group(1))]
        return "".join(chip(r, subject_id) for r in refs if r is not None)

    return Markup(CITE_RE.sub(replace, html))


def copy_text(markdown: str, handles: dict[str, SourceRef]) -> str:
    """The answer as plain text for the clipboard, with citations as [labels]."""

    def replace(match: re.Match) -> str:
        refs = [handles.get(h) for h in re.split(r"\s*[,;]\s*", match.group(1))]
        return "".join(f"[{r.label}]" for r in refs if r is not None)

    return CITE_RE.sub(replace, markdown)


def answer_meta(r: Answer) -> str:
    """ "Answered from Week 4 · 3 sources"."""
    verb = "Partly answered" if r.status == "partial" else "Answered"
    where = weeks_phrase(r.weeks).capitalize() if r.weeks else "your material"
    n = len(r.cited)
    return f"{verb} from {where} · {n} source{'' if n == 1 else 's'}"


templates.env.filters["cite"] = cite_html
templates.env.filters["chip"] = chip
templates.env.globals.update(
    copy_text=copy_text, answer_meta=answer_meta, ICONS=ICONS, SOURCE_TYPES=SOURCE_TYPES
)


# Pages


@router.get("/subjects/{subject_id}/ask", response_class=HTMLResponse)
def ask_new(request: Request, subject_id: uuid.UUID, db: DbSession) -> HTMLResponse:
    return _page(request, db, subject_or_404(db, subject_id), None)


@router.get("/subjects/{subject_id}/ask/{chat_id:uuid}", response_class=HTMLResponse)
def ask_chat(request: Request, subject_id: uuid.UUID, chat_id: uuid.UUID, db: DbSession):
    subject = subject_or_404(db, subject_id)
    return _page(request, db, subject, _chat_or_404(db, subject, chat_id))


@router.delete("/subjects/{subject_id}/ask/{chat_id:uuid}", response_class=HTMLResponse)
def delete_chat(
    request: Request, subject_id: uuid.UUID, chat_id: uuid.UUID, db: DbSession, current: int = 0
) -> Response:
    subject = subject_or_404(db, subject_id)
    db.delete(_chat_or_404(db, subject, chat_id))
    db.commit()
    if current:  # the open chat is gone: start a new one
        return Response(headers={"HX-Redirect": f"/subjects/{subject.id}/ask"})
    return templates.TemplateResponse(request, "_chat_sidebar.html", _sidebar(db, subject, None))


def _page(request: Request, db: Session, subject: Subject, chat: Chat | None) -> HTMLResponse:
    weeks = db.scalars(
        select(File.week).join(Chunk, Chunk.file_id == File.id)
        .where(Chunk.subject_id == subject.id, File.week.is_not(None))
        .distinct().order_by(File.week)
    ).all()  # fmt: skip
    return templates.TemplateResponse(
        request,
        "ask.html",
        {
            **_sidebar(db, subject, chat),
            "exchanges": chats.exchanges(chat) if chat else [],
            "weeks": weeks,
            "suggestions": [] if chat else chats.suggestions(db, subject),
        },
    )


def _sidebar(db: Session, subject: Subject, chat: Chat | None) -> dict:
    return {
        "subject": subject,
        "chat": chat,
        "groups": chats.chat_groups(db, subject),
        "stats": chats.index_stats(db, subject),
    }


def _chat_or_404(db: Session, subject: Subject, chat_id: uuid.UUID) -> Chat:
    chat = db.get(Chat, chat_id)
    if chat is None or chat.subject_id != subject.id:
        raise HTTPException(404, "Chat not found")
    return chat


# Asking


@router.post("/subjects/{subject_id}/ask/stream")
def ask_stream(
    subject_id: uuid.UUID, db: DbSession, service: AskDep, sessions: SessionsDep, form: FormLists
) -> StreamingResponse:
    subject = subject_or_404(db, subject_id)
    question = (form.get("q") or [""])[0].strip()
    if not question:
        raise HTTPException(422, "Type a question first")
    if len(question) > 1000:
        raise HTTPException(422, "Questions can be up to 1000 characters")
    mode = (form.get("mode") or ["auto"])[0]
    if mode not in ("auto", "answer", "where"):
        raise HTTPException(422, "Unknown mode")
    weeks = sorted({int(w) for w in form.get("week", []) if w.isdigit() and int(w) <= 52})
    types = [t for t in SOURCE_TYPES if t in form.get("type", [])]
    filters = Filters(tuple(weeks), tuple(types))

    chat = None
    if raw := (form.get("chat_id") or [""])[0]:
        try:
            chat = _chat_or_404(db, subject, uuid.UUID(raw))
        except ValueError as exc:
            raise HTTPException(404, "Chat not found") from exc
    created = chat is None
    if chat is None:
        chat = chats.new_chat(db, subject, question)
    turns = [] if created else chats.history(chat)
    chats.add_question(db, chat, question, mode, filters)
    db.commit()

    job = _Job(service, sessions, subject.id, chat.id, question, mode, filters, turns, created)
    return StreamingResponse(
        job.events(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class _Job:
    """One answer: a worker thread asks and saves; `events` streams what it reports."""

    def __init__(self, service, sessions, subject_id, chat_id, question, mode, filters, turns,
                 created):  # fmt: skip
        self.service, self.sessions = service, sessions
        self.subject_id, self.chat_id = subject_id, chat_id
        self.question, self.mode, self.filters, self.turns = question, mode, filters, turns
        self.created = created
        self.q: queue.Queue = queue.Queue()

    def work(self) -> None:
        try:
            with self.sessions() as db:
                subject = db.get(Subject, self.subject_id)
                reply = self.service.ask(
                    db, subject, self.question, self.filters, self.mode, self.turns,
                    on_event=lambda kind, data: self.q.put((kind, data)),
                )  # fmt: skip
                chats.add_reply(db, db.get(Chat, self.chat_id), reply)
                db.commit()
            self.q.put(("done", reply))
        except Exception as exc:
            logger.exception("answering failed in chat %s", self.chat_id)
            message = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
            try:
                with self.sessions() as db:
                    chats.add_error(db, db.get(Chat, self.chat_id), message)
                    db.commit()
            except Exception:
                logger.exception("couldn't save the error in chat %s", self.chat_id)
            self.q.put(("error", message))

    def events(self) -> Iterator[str]:
        # The database is only used here before the worker starts and after it's done
        # with it, never at the same time.
        start = {"type": "start", "chat_id": str(self.chat_id),
                 "url": f"/subjects/{self.subject_id}/ask/{self.chat_id}"}  # fmt: skip
        if self.created:
            start["sidebar"] = self._render_sidebar()
        threading.Thread(target=self.work, name="ask", daemon=True).start()
        yield _line(start)
        handles: dict[str, SourceRef] = {}
        pieces: list[str] = []
        last, topic = 0.0, None
        while True:
            kind, data = self.q.get()
            if kind == "where":
                topic = data
                yield _line({"type": "status", "text": f"Finding where {topic} is taught…"})
            elif kind == "sources":
                handles = data
                n = len(handles)
                text = (f"Checking {n} sources for {topic}…" if topic
                        else f"Reading {n} sources…")  # fmt: skip
                yield _line({"type": "status", "text": text})
            elif kind == "text":
                pieces.append(data)
                now = time.monotonic()
                if now - last < DELTA_INTERVAL:
                    continue
                markdown = partial_string("".join(pieces), "answer_markdown")
                if markdown:
                    last = now
                    html = cite_html(trim_partial_marker(markdown), handles, self.subject_id)
                    yield _line({"type": "delta", "html": str(html)})
            elif kind == "done":
                yield _line({"type": "done", "html": self._render_reply(data),
                             "sidebar": self._render_sidebar()})  # fmt: skip
                return
            else:
                html = templates.get_template("_msg_error.html").render(
                    error=data, question=self.question, mode=self.mode
                )
                yield _line({"type": "error", "html": html})
                return

    def _render_reply(self, reply: Answer | WhereAnswer) -> str:
        with self.sessions() as db:
            subject = db.get(Subject, self.subject_id)
            return templates.get_template("_reply.html").render(r=reply, subject=subject)

    def _render_sidebar(self) -> str:
        with self.sessions() as db:
            subject = db.get(Subject, self.subject_id)
            chat = db.get(Chat, self.chat_id)
            return templates.get_template("_chat_sidebar.html").render(
                **_sidebar(db, subject, chat)
            )


def _line(event: dict) -> str:
    return json.dumps(event) + "\n"


# Source preview panel


@router.get("/subjects/{subject_id}/ask/source/{chunk_id:uuid}", response_class=HTMLResponse)
def source_preview(
    request: Request, subject_id: uuid.UUID, chunk_id: uuid.UUID, db: DbSession
) -> HTMLResponse:
    subject = subject_or_404(db, subject_id)
    chunk = db.get(Chunk, chunk_id)
    if chunk is None or chunk.subject_id != subject.id:
        return _panel(request, {"subject": subject, "gone": True})
    meta = chunk.meta or {}
    highlight = (chunk.file_id, meta.get("idx")) if chunk.source_type == "transcript" else None
    if slide := meta.get("slide_id"):
        return _slide_panel(request, db, subject, uuid.UUID(slide), highlight,
                            notes=chunk.source_type == "notes")  # fmt: skip
    if chunk.source_type == "transcript":
        return _segment_panel(request, db, subject, chunk.file_id, meta.get("idx", 0))
    if chunk.source_type == "tutorial" and meta.get("section_id"):
        return _tutorial_panel(request, db, subject, uuid.UUID(meta["section_id"]))
    return _panel(request, {"subject": subject, "gone": True})


@router.get("/subjects/{subject_id}/ask/slide/{slide_id:uuid}", response_class=HTMLResponse)
def slide_preview(
    request: Request, subject_id: uuid.UUID, slide_id: uuid.UUID, db: DbSession
) -> HTMLResponse:
    return _slide_panel(request, db, subject_or_404(db, subject_id), slide_id, None)


def _slide_panel(request, db: Session, subject: Subject, slide_id: uuid.UUID, highlight,
                 notes: bool = False) -> HTMLResponse:  # fmt: skip
    slide = db.get(Slide, slide_id)
    if slide is not None and slide.duplicate_of:
        slide = db.get(Slide, slide.duplicate_of)
    file = db.get(File, slide.file_id) if slide else None
    if slide is None or file is None or file.subject_id != subject.id:
        return _panel(request, {"subject": subject, "gone": True})
    kept = db.execute(
        select(Slide.id, Slide.page_no)
        .where(Slide.file_id == file.id, Slide.duplicate_of.is_(None))
        .order_by(Slide.page_no)
    ).all()
    i = next(n for n, row in enumerate(kept) if row.id == slide.id)
    said = db.execute(
        select(TranscriptSegment, File)
        .join(File, File.id == TranscriptSegment.file_id)
        .where(TranscriptSegment.slide_id == slide.id)
        .order_by(File.filename, TranscriptSegment.idx)
    ).all()
    where = [f"Week {file.week}" if file.week is not None else None,
             f"Lecture {file.lecture}" if file.lecture is not None else None,
             f"slide {slide.page_no}"]  # fmt: skip
    return _panel(request, {
        "subject": subject, "slide": slide, "file": file, "said": said,
        "where": " · ".join(w for w in where if w), "deck_title": _file_title(file, subject),
        "prev": kept[i - 1] if i > 0 else None, "next": kept[i + 1] if i + 1 < len(kept) else None,
        "highlight": highlight, "notes_highlight": notes,
    })  # fmt: skip


def _segment_panel(request, db: Session, subject: Subject, file_id: uuid.UUID, idx: int):
    """A lecture moment that wasn't matched to a slide: it and its neighbours."""
    file = db.get(File, file_id)
    segments = db.scalars(
        select(TranscriptSegment)
        .where(
            TranscriptSegment.file_id == file_id, TranscriptSegment.idx.between(idx - 1, idx + 1)
        )
        .order_by(TranscriptSegment.idx)
    ).all()
    return _panel(request, {
        "subject": subject, "transcript": file, "segments": segments, "idx": idx,
        "where": f"Week {file.week} lecture" if file.week is not None else "Lecture",
        "deck_title": _file_title(file, subject),
    })  # fmt: skip


def _tutorial_panel(request, db: Session, subject: Subject, section_id: uuid.UUID):
    """A tutorial section: its labels, the lectures its concepts rely on, its text and
    solution."""
    section = db.get(TutorialSection, section_id)
    file = db.get(File, section.file_id) if section else None
    if section is None or file is None or file.subject_id != subject.id:
        return _panel(request, {"subject": subject, "gone": True})
    solution = db.get(TutorialSection, section.solution_section_id) if (
        section.solution_section_id) else None  # fmt: skip
    sheets = sheet_names([file], subject.code)
    where = [sheets.get(file.id) or _file_title(file, subject)]
    if file.week is not None:
        where.append(f"filed in Week {file.week}")
    return _panel(request, {
        "subject": subject, "section": section, "file": file, "solution": solution,
        "where": " · ".join(where),
    })  # fmt: skip


def _panel(request: Request, context: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, "_source_panel.html", context)
