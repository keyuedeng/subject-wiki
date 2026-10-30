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
from app.db.models import File, Slide, Subject, TranscriptSegment
from app.jobs.worker import can_process_transcript, can_queue
from app.parse.pipeline import can_parse
from app.parse.vision import PROMPT_VERSION
from app.qa.labels import clock
from app.services.board import (
    SLOTS,
    SUBJECT_SLOT_KEYS,
    WEEK_SLOT_KEYS,
    SlotError,
    build_board,
    resolve_slot,
    weeks_phrase,
)
from app.services.files import FileEdit, apply_edit, apply_type, delete_file
from app.storage.client import StorageError
from app.transcripts.pairing import (
    decks_for,
    pair_reason,
    pairs_by_subject,
    refresh_suggestions,
    reset_to_auto,
    set_manual,
    suggestions_by_subject,
    sync_auto_pairs,
)
from app.web import status as st


def _bind_storage(request: Request, storage: StorageDep) -> None:
    """Pages built by shared helpers need storage to refresh pairing suggestions (they
    read the transcript); keep it on the request rather than threading it through."""
    request.state.storage = storage


router = APIRouter(include_in_schema=False, dependencies=[Depends(_bind_storage)])
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


templates.env.filters["clock"] = clock
templates.env.filters["file_title"] = _file_title
templates.env.filters["markdown"] = _render_markdown
templates.env.globals["pair_reason"] = pair_reason
templates.env.globals.update(
    can_parse=can_parse,
    can_queue=can_queue,
    FLAG_LABELS={
        "exam_hint": "Exam hint",
        "worked_example": "Worked example",
        "analogy": "Analogy",
        "student_question": "Student question",
        "admin": "Admin",
    },
    is_transcript=can_process_transcript,
    FILE_TYPES=[t.value for t in FileType],
    TYPE_LABELS=st.TYPE_LABELS,
    ACCEPTED_LABEL=", ".join(e.upper() for e in sorted(patterns.ACCEPTED_EXTENSIONS)),
    weeks_phrase=weeks_phrase,
    SLOTS=SLOTS,
    WEEK_SLOT_KEYS=WEEK_SLOT_KEYS,
    SUBJECT_SLOT_KEYS=SUBJECT_SLOT_KEYS,
)


async def form_data(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


FormData = Annotated[dict[str, str], Depends(form_data)]


async def form_lists(request: Request) -> dict[str, list[str]]:
    """Every value of every field (checkbox groups send a name more than once)."""
    out: dict[str, list[str]] = {}
    for k, v in (await request.form()).multi_items():
        if isinstance(v, str):
            out.setdefault(k, []).append(v)
    return out


FormLists = Annotated[dict[str, list[str]], Depends(form_lists)]


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
    request: Request, subject_id: uuid.UUID, db: DbSession, view: str = "board", filter: str = "all"
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
            "upload_accept": ",".join(f".{e}" for e in sorted(patterns.ACCEPTED_EXTENSIONS)),
            **_view_context(db, subject, view, filter, request.state.storage),
        },
    )


@router.get("/subjects/{subject_id}/view/{view}", response_class=HTMLResponse)
def subject_view(
    request: Request, subject_id: uuid.UUID, view: str, db: DbSession, filter: str = "all"
) -> HTMLResponse:
    subject = subject_or_404(db, subject_id)
    return _render_view(request, db, subject, view, filter)


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
    request: Request,
    file_id: uuid.UUID,
    db: DbSession,
    storage: StorageDep,
    view: str = "table",
    filter: str = "all",
) -> Response:
    file = file_or_404(db, file_id)
    subject = file.subject
    try:
        delete_file(db, storage, file)
    except StorageError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _render_view(request, db, subject, view, filter)


# Slide parsing


@router.post("/files/{file_id}/parse", response_class=HTMLResponse)
def parse_file(
    request: Request, file_id: uuid.UUID, db: DbSession, queue: QueueDep, view: str = "row"
) -> Response:
    file = file_or_404(db, file_id)
    if not can_queue(file):
        raise HTTPException(422, "Only slides (PDF, PPTX) and transcripts can be processed")
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
    return _row(request, db, file)


@router.get("/slides/{slide_id}")
def slide_link(slide_id: uuid.UUID, db: DbSession) -> Response:
    """Permalink to a slide. A collapsed build step or repeat resolves to the slide it
    was collapsed into."""
    slide = db.get(Slide, slide_id)
    if slide is None:
        raise HTTPException(404, "Slide not found")
    kept = db.get(Slide, slide.duplicate_of) if slide.duplicate_of else slide
    return RedirectResponse(f"/files/{kept.file_id}/slides?page={kept.page_no}", status_code=303)


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
    kept = by_id.get(current.duplicate_of) or current
    said = db.execute(
        select(TranscriptSegment, File)
        .join(File, File.id == TranscriptSegment.file_id)
        .where(TranscriptSegment.slide_id == kept.id)
        .order_by(File.filename, TranscriptSegment.idx)
    ).all()
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
            "said": said,
            "prev": shown[i - 1] if i > 0 else None,
            "next": shown[i + 1] if i + 1 < len(shown) else None,
            "only": only,
            "stats": stats,
        },
    )


# Transcripts


@router.get("/files/{file_id}/transcript", response_class=HTMLResponse)
def transcript_view(
    request: Request, file_id: uuid.UUID, db: DbSession, show: str = "clean", flag: str = ""
) -> HTMLResponse:
    file = file_or_404(db, file_id)
    if not can_process_transcript(file):
        raise HTTPException(404, "Not a transcript")
    sync_auto_pairs(db, file.subject_id)
    refresh_suggestions(db, request.state.storage, file.subject_id)
    decks = decks_for(db, file.id)
    segments = list(
        db.scalars(
            select(TranscriptSegment)
            .where(TranscriptSegment.file_id == file.id)
            .order_by(TranscriptSegment.idx)
        )
    )
    slides = {s.id: s for s in db.scalars(select(Slide).where(
        Slide.id.in_({seg.slide_id for seg in segments if seg.slide_id})))}  # fmt: skip
    deck_names = {}
    for f, _ in decks:
        deck_names[f.id] = _file_title(f, file.subject)
    for s in slides.values():
        if s.file_id not in deck_names:
            deck_names[s.file_id] = _file_title(db.get(File, s.file_id), file.subject)
    shown = [seg for seg in segments if not flag or any(f["type"] == flag for f in seg.flags)]
    runs: list[dict] = []
    for seg in shown:
        if runs and runs[-1]["slide_id"] == seg.slide_id and not flag:
            runs[-1]["segments"].append(seg)
        else:
            runs.append({"slide_id": seg.slide_id, "slide": slides.get(seg.slide_id),
                         "segments": [seg]})  # fmt: skip
    used_decks = {s.file_id for s in slides.values()}
    paired = [f.id for f, _ in decks]
    flag_counts = {}
    for seg in segments:
        for f in seg.flags:
            flag_counts[f["type"]] = flag_counts.get(f["type"], 0) + 1
    stats = {
        "windows": len(segments),
        "aligned": sum(seg.slide_id is not None for seg in segments),
        "rejected": sum(seg.clean_status == "rejected" for seg in segments),
        "this_run": sum(seg.cost_usd or 0 for seg in segments if not seg.cached),
        "full_cost": sum(seg.cost_usd or 0 for seg in segments),
        "cached": sum(seg.cached for seg in segments),
        "prompt_versions": sorted({seg.prompt_version for seg in segments if seg.prompt_version}),
    }
    subject_decks = [f for f in _files(db, file.subject) if can_parse(f) and f.id not in paired]
    suggestion = suggestions_by_subject(db, file.subject_id).get(file.id)
    suggested = db.get(File, suggestion.slides_file_id) if suggestion else None
    return templates.TemplateResponse(
        request,
        "transcript.html",
        {
            "file": file,
            "subject": file.subject,
            "decks": decks,
            "deck_names": deck_names,
            "subject_decks": sorted(subject_decks, key=lambda f: f.filename.lower()),
            "suggestion": suggestion,
            "suggested": suggested,
            "runs": runs,
            "stats": stats,
            "flag_counts": flag_counts,
            "show": show,
            "flag": flag,
            # Processed against decks that are no longer the pairing: offer to re-run.
            "stale_pairing": bool(segments) and bool(used_decks - set(paired)),
        },
    )


@router.post("/files/{file_id}/pairing/{action}")
def change_pairing(
    request: Request,
    file_id: uuid.UUID,
    action: str,
    db: DbSession,
    queue: QueueDep,
    form: FormData,
    view: str = "",
) -> Response:
    """add / remove / reset, from the transcript view (redirects back to it), or confirm a
    suggested deck: pairs it and processes the transcript; with `view` (board or row) the
    updated board or table row is sent back."""
    file = file_or_404(db, file_id)
    if not can_process_transcript(file):
        raise HTTPException(404, "Not a transcript")
    sync_auto_pairs(db, file.subject_id)  # so "add" keeps an automatic pairing
    current = [f.id for f, _ in decks_for(db, file.id)]
    if action == "reset":
        reset_to_auto(db, file)
    else:
        try:
            deck_id = uuid.UUID(form.get("slides_id", ""))
        except ValueError as exc:
            raise HTTPException(422, "Pick a slides file") from exc
        deck = db.get(File, deck_id)
        if deck is None or deck.subject_id != file.subject_id or not can_parse(deck):
            raise HTTPException(422, "Pick a slides file from this subject")
        if action == "confirm":
            set_manual(db, file.id, [deck_id])
            queue.enqueue(db, [file])
            db.refresh(file)
            if view in ("board", "row"):
                return _after_change(request, db, file, view)
        elif action == "add":
            set_manual(db, file.id, [*current, deck_id])
        elif action == "remove":
            set_manual(db, file.id, [d for d in current if d != deck_id])
        else:
            raise HTTPException(404, "Unknown action")
    return RedirectResponse(f"/files/{file.id}/transcript", status_code=303)


# Helpers


def _pair_names(db: DbSession, subject: Subject) -> dict:
    """transcript id -> titles of its paired decks."""
    files = {f.id: f for f in _files(db, subject)}
    return {
        t: [_file_title(files[d], subject) for d in decks if d in files]
        for t, decks in pairs_by_subject(db, subject.id).items()
    }


def _row(request: Request, db: DbSession, file: File) -> HTMLResponse:
    subject = file.subject
    _refresh_pairing(db, subject, request.state.storage)
    files = _files(db, subject)
    board = build_board(subject, files, pairs_by_subject(db, subject.id))
    return templates.TemplateResponse(
        request,
        "_file_row.html",
        {"f": file, "subject": subject, "statuses": _statuses(db, subject, files, board),
         "names": _names(db, subject, files), "filter": "all"},
    )  # fmt: skip


def _names(db: DbSession, subject: Subject, files: list[File]) -> dict:
    """Board and table titles that differ from file_title: a transcript without a title of
    its own is named after the deck it's paired with."""
    pairs = _pair_names(db, subject)
    return {
        f.id: f"Lecture transcript: {pairs[f.id][0]}"
        for f in files
        if f.type == FileType.TRANSCRIPT and not f.title and pairs.get(f.id)
    }


def _refresh_pairing(db: DbSession, subject: Subject, storage) -> None:
    sync_auto_pairs(db, subject.id)
    if storage is not None:
        refresh_suggestions(db, storage, subject.id)


def _statuses(db: DbSession, subject: Subject, files: list[File], board) -> dict:
    """File id -> its status line on the board and in the table."""
    names = _pair_names(db, subject)
    titles = {f.id: _file_title(f, subject) for f in files}
    tutorials = st.tutorial_pairs(board, titles)
    lengths = st.durations(db, [f.id for f in files if f.type == FileType.TRANSCRIPT])
    suggested = {
        t: st.Suggested(s.slides_file_id, titles.get(s.slides_file_id, ""), s.score, s.runner_up)
        for t, s in suggestions_by_subject(db, subject.id).items()
    }
    return {
        f.id: st.file_status(f, duration_ms=lengths.get(f.id), paired_decks=names.get(f.id),
                             tutorial=tutorials.get(f.id), suggestion=suggested.get(f.id))
        for f in files
    }  # fmt: skip


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
        sync_auto_pairs(db, subject.id)
        files = files_by_subject[subject.id]
        cards.append(
            {
                "subject": subject,
                "files": len(files),
                "review": sum(f.needs_review for f in files),
                "complete_weeks": (
                    build_board(subject, files, pairs_by_subject(db, subject.id)).complete_weeks
                    if files
                    else 0
                ),
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


def _view_context(
    db: DbSession, subject: Subject, view: str, filter: str = "all", storage=None
) -> dict:
    _refresh_pairing(db, subject, storage)
    files = _files(db, subject)
    summary = {
        "total": len(files),
        "review": sum(f.needs_review for f in files),
        "excluded": sum(not f.included for f in files),
        "transcripts": sum(f.type == FileType.TRANSCRIPT for f in files),
        "timestamped": sum(f.has_timestamps for f in files),
        "slides": sum(can_parse(f) for f in files),
        "unparsed": sum(can_queue(f) and f.status in ("uploaded", "failed") for f in files),
        "parsing": sum(f.status == "parsing" for f in files),
    }
    board = build_board(subject, files, pairs_by_subject(db, subject.id))
    context = {
        "subject": subject,
        "summary": summary,
        "view": view,
        "statuses": _statuses(db, subject, files, board),
        "names": _names(db, subject, files),
    }
    if view == "table":
        files.sort(
            key=lambda f: (f.week is None, f.week or 0, f.lecture or 0, f.type, f.filename.lower())
        )
        filters = [("all", "All", len(files))]
        if summary["review"]:
            filters.append(("review", "Needs a check", summary["review"]))
        for key, label, types in st.TABLE_FILTERS:
            if n := sum(f.type in types for f in files):
                filters.append((key, label, n))
        if filter not in {k for k, _, _ in filters}:
            filter = "all"
        types = dict((k, t) for k, _, t in st.TABLE_FILTERS)
        if filter == "review":
            files = [f for f in files if f.needs_review]
        elif filter in types:
            files = [f for f in files if f.type in types[filter]]
        context.update(files=files, filters=filters, filter=filter)
    else:
        context["board"] = board
    return context


def _render_view(
    request: Request, db: DbSession, subject: Subject, view: str, filter: str = "all"
) -> HTMLResponse:
    if view not in Views:
        raise HTTPException(404, "Unknown view")
    context = _view_context(db, subject, view, filter, request.state.storage)
    return templates.TemplateResponse(request, f"_{view}.html", context)


def _after_change(request: Request, db: DbSession, file: File, view: str) -> HTMLResponse:
    if view == "row":
        return _row(request, db, file)
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
