"""Server-rendered pages and HTMX partials. Logic lives in services/; these only render."""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from pydantic import ValidationError
from sqlalchemy import select

from app.api.deps import DbSession, QueueDep, StorageDep, file_or_404, subject_or_404
from app.api.schemas import SubjectCreate
from app.classify import patterns
from app.classify.titles import display_title
from app.classify.types import FileType
from app.config import get_settings
from app.db.models import File, Slide, Subject
from app.parse.pipeline import can_parse
from app.parse.vision import PROMPT_VERSION
from app.services.board import (
    SLOTS,
    SUBJECT_SLOT_KEYS,
    WEEK_SLOT_KEYS,
    SlotError,
    build_board,
    resolve_slot,
)
from app.services.files import FileEdit, apply_edit, apply_type, delete_file
from app.storage.client import StorageError

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

Views = ("board", "table")


def _filesize(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def _date(d: datetime | None) -> str:
    return d.astimezone().strftime("%-d %b %Y, %H:%M") if d else ""


def _file_title(f: File, subject: Subject | None) -> str:
    code = subject.code if subject is not None else None
    return display_title(f.title, f.filename, FileType(f.type), f.week, f.lecture, code)


templates.env.filters["filesize"] = _filesize
templates.env.filters["date"] = _date
# Markdown from parsed slides: raw HTML disabled, so model output can't inject markup.
_markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
# "- + getCode() : int": a UML visibility marker after a bullet would start a nested list.
_VISIBILITY_AFTER_BULLET = re.compile(r"^(\s*[-*+]\s+)([+\-#~])(?=\s)", re.MULTILINE)


def _render_markdown(text: str | None) -> str:
    return _markdown.render(_VISIBILITY_AFTER_BULLET.sub(r"\1\\\2", text or ""))


templates.env.filters["file_title"] = _file_title
templates.env.filters["markdown"] = _render_markdown
templates.env.globals.update(
    can_parse=can_parse,
    FILE_TYPES=[t.value for t in FileType],
    SLOTS=SLOTS,
    WEEK_SLOT_KEYS=WEEK_SLOT_KEYS,
    SUBJECT_SLOT_KEYS=SUBJECT_SLOT_KEYS,
)


async def form_data(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


FormData = Annotated[dict[str, str], Depends(form_data)]


# Pages


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: DbSession) -> HTMLResponse:
    return _index(request, db)


@router.post("/subjects")
def create_subject(request: Request, db: DbSession, form: FormData) -> Response:
    try:
        body = SubjectCreate(**form)
    except ValidationError as exc:
        errors = "; ".join(f"{e['loc'][-1]}: {e['msg']}" for e in exc.errors())
        return _index(request, db, error=errors, form=form, status_code=422)  # reopens dialog
    subject = Subject(**body.model_dump())
    db.add(subject)
    db.commit()
    return RedirectResponse(f"/subjects/{subject.id}", status_code=303)


@router.get("/subjects/{subject_id}", response_class=HTMLResponse)
def subject_page(
    request: Request, subject_id: uuid.UUID, db: DbSession, view: str = "board"
) -> HTMLResponse:
    subject = subject_or_404(db, subject_id)
    view = view if view in Views else "board"
    settings = get_settings()
    upload_rules = {
        "accepted": sorted(patterns.ACCEPTED_EXTENSIONS),
        "audioVideo": sorted(patterns.AUDIO_VIDEO_EXTENSIONS),
        "maxBytes": settings.max_upload_bytes,
    }
    return templates.TemplateResponse(
        request,
        "subject.html",
        {
            "subject": subject,
            "view": view,
            "upload_rules": json.dumps(upload_rules),
            **_view_context(db, subject, view),
        },
    )


@router.get("/subjects/{subject_id}/view/{view}", response_class=HTMLResponse)
def subject_view(request: Request, subject_id: uuid.UUID, view: str, db: DbSession) -> HTMLResponse:
    subject = subject_or_404(db, subject_id)
    return _render_view(request, db, subject, view)


# File actions (HTMX). `view` says which partial to send back.


@router.patch("/files/{file_id}", response_class=HTMLResponse)
def patch_file(request: Request, file_id: uuid.UUID, db: DbSession, form: FormData) -> Response:
    file = file_or_404(db, file_id)
    apply_edit(file, _parse_edit(form))
    db.commit()
    return _after_change(request, db, file, form.get("view", "table"))


@router.post("/files/{file_id}/move", response_class=HTMLResponse)
def move_file(request: Request, file_id: uuid.UUID, db: DbSession, form: FormData) -> Response:
    file = file_or_404(db, file_id)
    week = _parse_int(form.get("week", ""), "week", 52)
    try:
        new_type, file.week = resolve_slot(form.get("slot", ""), week, FileType(file.type))
    except SlotError as exc:
        raise HTTPException(422, str(exc)) from exc
    apply_type(file, new_type)
    file.reviewed = True
    db.commit()
    # The browser has already moved the chip; the re-rendered board regroups lectures
    # (a transcript may now pair with slides) and updates the week's completeness.
    return _render_view(request, db, file.subject, "board")


@router.delete("/files/{file_id}", response_class=HTMLResponse)
def remove_file(
    request: Request, file_id: uuid.UUID, db: DbSession, storage: StorageDep, view: str = "table"
) -> Response:
    file = file_or_404(db, file_id)
    subject = file.subject
    try:
        delete_file(db, storage, file)
    except StorageError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _render_view(request, db, subject, view)


# Slide parsing


@router.post("/files/{file_id}/parse", response_class=HTMLResponse)
def parse_file(
    request: Request, file_id: uuid.UUID, db: DbSession, queue: QueueDep, view: str = "row"
) -> Response:
    file = file_or_404(db, file_id)
    if not can_parse(file):
        raise HTTPException(422, "Only slides (PDF or PPTX) can be parsed")
    queue.enqueue(db, [file])
    db.refresh(file)  # the worker may already have moved it on
    return _after_change(request, db, file, view)


@router.post("/subjects/{subject_id}/parse", response_class=HTMLResponse)
def parse_subject(
    request: Request, subject_id: uuid.UUID, db: DbSession, queue: QueueDep, view: str = "board"
) -> Response:
    subject = subject_or_404(db, subject_id)
    files = [f for f in _files(db, subject) if f.status in ("uploaded", "failed")]
    queue.enqueue(db, files)
    return _render_view(request, db, subject, view)


@router.get("/files/{file_id}/row", response_class=HTMLResponse)
def file_row(request: Request, file_id: uuid.UUID, db: DbSession) -> HTMLResponse:
    """Polled by table rows while a file is parsing."""
    file = file_or_404(db, file_id)
    return templates.TemplateResponse(
        request, "_file_row.html", {"f": file, "subject": file.subject}
    )


@router.get("/files/{file_id}/pages/{page_no}.png")
def page_image(file_id: uuid.UUID, page_no: int, db: DbSession, storage: StorageDep) -> Response:
    """Rendered pages, served through the app so the bucket stays private."""
    slide = db.scalar(select(Slide).where(Slide.file_id == file_id, Slide.page_no == page_no))
    if slide is None:
        raise HTTPException(404, "Page not found")
    try:
        png = storage.get(slide.image_path)
    except StorageError as exc:
        raise HTTPException(502, str(exc)) from exc
    # Content-addressed, so the bytes for this URL never change.
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=86400"})


@router.get("/files/{file_id}/slides", response_class=HTMLResponse)
def slide_viewer(
    request: Request, file_id: uuid.UUID, db: DbSession, page: int = 1, only: str = ""
) -> HTMLResponse:
    file = file_or_404(db, file_id)
    slides = file.slides
    if not slides:
        return templates.TemplateResponse(
            request, "viewer.html", {"file": file, "subject": file.subject, "slide": None}
        )
    shown = [s for s in slides if s.route == "vision"] if only == "vision" else slides
    shown = shown or slides
    current = next((s for s in shown if s.page_no >= page), shown[-1])
    i = shown.index(current)
    by_id = {s.id: s for s in slides}
    vision = [s for s in slides if s.route == "vision"]
    stats = {
        "pages": len(slides),
        "vision": len(vision),
        "text": sum(s.route == "text" for s in slides),
        "duplicate": sum(s.route == "duplicate" for s in slides),
        "cached": sum(s.cached for s in vision),
        "this_parse": sum(s.cost_usd or 0 for s in vision if not s.cached),
        "full_cost": sum(s.cost_usd or 0 for s in vision),
        "unknown_price": any(s.cost_usd is None for s in vision),
        "models": sorted({s.model for s in vision if s.model}),
        "prompt_versions": sorted({s.prompt_version for s in vision if s.prompt_version}),
        "current_prompt": PROMPT_VERSION,
    }
    return templates.TemplateResponse(
        request,
        "viewer.html",
        {
            "file": file,
            "subject": file.subject,
            "slides": slides,
            "slide": current,
            "content": current.content_json,
            "keeper": by_id.get(current.duplicate_of),
            "prev": shown[i - 1] if i > 0 else None,
            "next": shown[i + 1] if i + 1 < len(shown) else None,
            "only": only,
            "stats": stats,
        },
    )


# Helpers


def _index(
    request: Request,
    db: DbSession,
    error: str | None = None,
    form: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    subjects = list(db.scalars(select(Subject).order_by(Subject.created_at.desc())))
    files_by_subject: dict[uuid.UUID, list[File]] = {s.id: [] for s in subjects}
    for f in db.scalars(select(File)):
        files_by_subject.setdefault(f.subject_id, []).append(f)
    cards = []
    for subject in subjects:
        files = files_by_subject[subject.id]
        cards.append(
            {
                "subject": subject,
                "files": len(files),
                "review": sum(f.needs_review for f in files),
                "complete_weeks": build_board(subject, files).complete_weeks if files else 0,
            }
        )
    return templates.TemplateResponse(
        request,
        "index.html",
        {"cards": cards, "error": error, "form": form or {}},
        status_code=status_code,
    )


def _files(db: DbSession, subject: Subject) -> list[File]:
    return list(db.scalars(select(File).where(File.subject_id == subject.id)))


def _view_context(db: DbSession, subject: Subject, view: str) -> dict:
    files = _files(db, subject)
    summary = {
        "total": len(files),
        "review": sum(f.needs_review for f in files),
        "excluded": sum(not f.included for f in files),
        "transcripts": sum(f.type == FileType.TRANSCRIPT for f in files),
        "timestamped": sum(f.has_timestamps for f in files),
        "slides": sum(can_parse(f) for f in files),
        "unparsed": sum(can_parse(f) and f.status in ("uploaded", "failed") for f in files),
        "parsing": sum(f.status == "parsing" for f in files),
    }
    context = {"subject": subject, "summary": summary, "view": view}
    if view == "table":
        files.sort(
            key=lambda f: (f.week is None, f.week or 0, f.lecture or 0, f.type, f.filename.lower())
        )
        context["files"] = files
    else:
        context["board"] = build_board(subject, files)
    return context


def _render_view(request: Request, db: DbSession, subject: Subject, view: str) -> HTMLResponse:
    if view not in Views:
        raise HTTPException(404, "Unknown view")
    return templates.TemplateResponse(request, f"_{view}.html", _view_context(db, subject, view))


def _after_change(request: Request, db: DbSession, file: File, view: str) -> HTMLResponse:
    if view == "row":
        return templates.TemplateResponse(
            request, "_file_row.html", {"f": file, "subject": file.subject}
        )
    return _render_view(request, db, file.subject, view)


def _parse_int(raw: str, field: str, max_value: int) -> int | None:
    raw = raw.strip()
    if not raw:
        return None
    if not raw.isdigit() or int(raw) > max_value:
        raise HTTPException(422, f"{field} must be a whole number from 0 to {max_value}")
    return int(raw)


def _parse_edit(form: dict[str, str]) -> FileEdit:
    edit = FileEdit()
    if "type" in form:
        if form["type"] not in FileType:
            raise HTTPException(422, f"Unknown type '{form['type']}'")
        edit.type = form["type"]
    if "week" in form:
        edit.week = _parse_int(form["week"], "Week", 52)
    if "lecture" in form:
        edit.lecture = _parse_int(form["lecture"], "Lecture", 99)
    if "included" in form:
        edit.included = form["included"] == "true"
    if "reviewed" in form:
        edit.reviewed = form["reviewed"] == "true"
    return edit
