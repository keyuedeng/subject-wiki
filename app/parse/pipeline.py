"""Parse one slides file: load -> measure -> dedupe -> route -> render -> vision/text -> rows.

Vision results are cached by a hash of the full request (app/llm/cache.py) and stored as
soon as each page finishes, so a failure part-way loses nothing: re-running only pays
for the pages that didn't finish. Re-parsing an unchanged file makes no model calls, and
an edit that changes only some requests re-runs only those pages.
"""

import logging
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.classify.titles import document_title
from app.classify.types import FileStatus, FileType
from app.db.models import File, Slide, slide_id
from app.jobs.lease import Leases, held
from app.llm.cache import CallResult, LlmCache, image_sha256, request_key
from app.parse.dedupe import Duplicate, find_duplicates
from app.parse.document import (
    ConversionError,
    Deck,
    PageInfo,
    deck_from_pdf,
    docx_to_pdf,
    pptx_notes,
    pptx_to_pdf,
    render_page,
)
from app.parse.pricing import cost_usd
from app.parse.routing import Decision, route_page
from app.parse.schema import SlideContent
from app.parse.text_path import text_content
from app.parse.vision import PROMPT_VERSION, Usage, VisionModel, VisionResult
from app.storage.client import Storage, StorageNotFound, converted_pdf_path, page_image_path

logger = logging.getLogger(__name__)

PARSEABLE_EXTS = {"pdf", "pptx"}


class ParseError(Exception):
    pass


@dataclass
class PageCall:
    """The vision outcome for one page."""

    result: VisionResult
    request_hash: str
    cached: bool
    prompt_version: str | None
    cost_usd: float | None


def can_parse(file: File) -> bool:
    return file.type == FileType.SLIDES.value and file.ext in PARSEABLE_EXTS


@dataclass
class Parser:
    session_factory: Callable[[], Session]
    storage: Storage
    vision_factory: Callable[[], VisionModel]
    concurrency: int = 4
    # Called after a successful parse (e.g. to re-align transcripts paired with this deck).
    on_parsed: Callable[[uuid.UUID], None] | None = None
    # Set by the queue; skips the parse if another process is working on the file.
    leases: Leases | None = None

    def parse(self, file_id: uuid.UUID) -> None:
        """Run a parse and record the outcome on the file; never raises."""
        with held(self.leases, file_id, "parse") as ok:
            if ok:
                self._run(file_id)

    def _run(self, file_id: uuid.UUID) -> None:
        try:
            summary = self._parse(file_id)
        except Exception as exc:
            logger.exception("parse failed for file %s", file_id)
            message = (
                str(exc)
                if isinstance(exc, ParseError | ConversionError)
                else (f"{type(exc).__name__}: {exc}")
            )
            self._set_status(file_id, FileStatus.FAILED, message[:500])
        else:
            self._set_status(file_id, FileStatus.PARSED, summary)
            if self.on_parsed is not None:
                self.on_parsed(file_id)

    # Steps

    def _parse(self, file_id: uuid.UUID) -> str:
        with self.session_factory() as db:
            file = db.get(File, file_id)
            if file is None:
                raise ParseError("File no longer exists")
            sha, ext, path, subject_id = file.sha256, file.ext, file.storage_path, file.subject_id

        deck = self._load_deck(sha, ext, self.storage.get(path))
        pages = deck.pages
        if not pages:
            raise ParseError("The document has no pages")
        duplicates = {d.page_no: d for d in find_duplicates(pages)}
        decisions = {p.page_no: route_page(p) for p in pages}
        vision_pages = [
            p for p in pages if p.page_no not in duplicates
            and decisions[p.page_no].route == "vision"
        ]  # fmt: skip

        pngs = self._render_and_store(deck, sha, {p.page_no for p in vision_pages})
        calls, errors = self._vision(file_id, deck, vision_pages, duplicates, pngs)

        with self.session_factory() as db:
            self._write_slides(db, file_id, subject_id, sha, deck, duplicates, decisions, calls)
            file = db.get(File, file_id)
            file.page_count = len(pages)
            # The parsed first pages name the deck better than the upload-time peek did.
            first_titles = [
                s.title for s in sorted(file.slides, key=lambda s: s.page_no)[:3] if s.title
            ]
            if title := document_title(first_titles, file.subject.code, file.subject.name):
                file.title = title
            db.commit()

        if errors:
            first = next(iter(errors.values()))
            raise ParseError(
                f"{len(errors)} of {len(vision_pages)} vision pages failed "
                f"(finished pages are saved; parse again to retry). First error: {first}"
            )
        return _summary(pages, duplicates, calls)

    def _load_deck(self, sha: str, ext: str, data: bytes) -> Deck:
        if ext == "pdf":
            return deck_from_pdf(data)
        if ext == "docx":
            return deck_from_pdf(self._converted_pdf(sha, data, docx_to_pdf), exact_spaces=True)
        if ext != "pptx":
            raise ConversionError(f"Only PDF, PPTX and DOCX can be parsed, not .{ext}")
        notes, visible = pptx_notes(data)
        return deck_from_pdf(self._converted_pdf(sha, data), notes, expected_pages=visible)

    def _converted_pdf(self, sha: str, data: bytes, convert=None) -> bytes:
        """LibreOffice output is stored by the source file's hash, so it converts once."""
        path = converted_pdf_path(sha)
        try:
            return self.storage.get(path)
        except StorageNotFound:
            pdf = (convert or pptx_to_pdf)(data)
            self.storage.put(path, pdf, "application/pdf")
            return pdf

    def read_document(
        self, file_id: uuid.UUID, sha: str, ext: str, data: bytes
    ) -> tuple[Deck, dict[int, "PageCall"], dict[int, str]]:
        """A document's pages with vision results for the pages routing sends to vision
        (cached like slides). Used for tutorial sheets: no build-step dedupe, and page
        images are stored for the viewer."""
        deck = self._load_deck(sha, ext, data)
        vision_pages = [p for p in deck.pages if route_page(p).route == "vision"]
        pngs = self._render_and_store(deck, sha, {p.page_no for p in vision_pages})
        calls, errors = self._vision(file_id, deck, vision_pages, {}, pngs)
        return deck, calls, errors

    def _render_and_store(self, deck: Deck, sha: str, keep: set[int]) -> dict[int, bytes]:
        """Upload any missing page images; return the PNGs of the pages in `keep`."""
        existing = set(self.storage.list(f"pages/{sha}"))
        pngs: dict[int, bytes] = {}
        with pymupdf.open(stream=deck.pdf, filetype="pdf") as doc:
            for page in deck.pages:
                path = page_image_path(sha, page.page_no)
                if path in existing and page.page_no not in keep:
                    continue
                png = render_page(doc, page.page_no)
                if path not in existing:
                    self.storage.put(path, png, "image/png")
                if page.page_no in keep:
                    pngs[page.page_no] = png
        return pngs

    def _vision(
        self,
        file_id: uuid.UUID,
        deck: Deck,
        pages: list[PageInfo],
        duplicates: dict[int, Duplicate],
        pngs: dict[int, bytes],
    ) -> tuple[dict[int, PageCall], dict[int, str]]:
        calls: dict[int, PageCall] = {}
        errors: dict[int, str] = {}
        if not pages:
            return calls, errors

        vision = self.vision_factory()
        cache = LlmCache(self.session_factory)
        by_no = {p.page_no: p for p in deck.pages}
        captions = {p.page_no: _build_captions(p, by_no, duplicates) for p in pages}
        keys = {
            p.page_no: request_key(
                vision.fingerprint(image_sha256(pngs[p.page_no]), p.text_raw, captions[p.page_no])
            )
            for p in pages
        }
        hits = cache.get_many(keys.values())
        for p in pages:
            if hit := hits.get(keys[p.page_no]):
                calls[p.page_no] = PageCall(
                    result=VisionResult(
                        content=SlideContent.model_validate(hit.response),
                        usage=Usage(hit.input_tokens, hit.cached_input_tokens,
                                    hit.output_tokens, 0),
                        model=vision.model,
                    ),
                    request_hash=keys[p.page_no],
                    cached=True,
                    prompt_version=hit.prompt_version,
                    cost_usd=hit.cost_usd,
                )  # fmt: skip

        todo = [p for p in pages if p.page_no not in calls]
        if not todo:
            return calls, errors

        self._progress(file_id, done=0, total=len(todo))
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {
                pool.submit(vision.describe, pngs[p.page_no], p.text_raw, captions[p.page_no]): p
                for p in todo
            }
            for done, future in enumerate(as_completed(futures), 1):
                n = futures[future].page_no
                try:
                    result = future.result()
                except Exception as exc:
                    logger.warning("vision call failed on page %s: %s", n, exc)
                    errors[n] = f"page {n}: {type(exc).__name__}: {exc}"[:300]
                    continue
                u = result.usage
                cost = cost_usd(vision.model, u.input_tokens, u.cached_tokens, u.output_tokens)
                calls[n] = PageCall(result, keys[n], False, PROMPT_VERSION, cost)
                if result.content is not None:  # refusals aren't cached; they may pass later
                    cache.put(
                        keys[n],
                        CallResult(
                            response=result.content.model_dump(mode="json"),
                            input_tokens=u.input_tokens,
                            cached_input_tokens=u.cached_tokens,
                            output_tokens=u.output_tokens,
                            cost_usd=cost,
                        ),
                        kind="slide_vision",
                        provider=vision.provider,
                        model=vision.model,
                        prompt_version=PROMPT_VERSION,
                    )
                self._progress(file_id, done=done, total=len(todo))
        return calls, errors

    def _write_slides(
        self,
        db: Session,
        file_id: uuid.UUID,
        subject_id: uuid.UUID,
        sha: str,
        deck: Deck,
        duplicates: dict[int, Duplicate],
        decisions: dict[int, Decision],
        calls: dict[int, PageCall],
    ) -> None:
        """Rows are updated in place under derived ids (subject, sha256, page), so
        anything pointing at a slide (transcript segments) survives a re-parse."""
        ids = {p.page_no: slide_id(subject_id, sha, p.page_no) for p in deck.pages}
        existing = {r.id: r for r in db.scalars(select(Slide).where(Slide.file_id == file_id))}
        stale = [r for i, r in existing.items() if i not in ids.values()]
        for r in stale:  # pages that no longer exist, or rows from before derived ids
            db.delete(r)
        db.flush()
        rows = []
        for page in deck.pages:
            n = page.page_no
            row = existing.get(ids[n]) or Slide(id=ids[n], file_id=file_id, page_no=n)
            row.text_raw = page.text_raw
            row.notes = deck.notes.get(n)
            row.route_signals = page.signals
            row.image_path = page_image_path(sha, n)
            row.duplicate_of = None
            row.model = row.prompt_version = row.request_hash = None
            row.input_tokens = row.cached_input_tokens = row.output_tokens = 0
            row.cost_usd, row.cached = None, False
            content = text_content(page)
            if n in duplicates:
                dup = duplicates[n]
                row.route, row.route_reason = "duplicate", f"{dup.reason} (kept page {dup.of})"
                row.duplicate_of = ids[dup.of]
            elif decisions[n].route == "vision" and n in calls:
                call = calls[n]
                result = call.result
                if result.content is None:
                    row.route = "text"
                    row.route_reason = (
                        f"{decisions[n].reason}; model refused ({result.refusal}), used text layer"
                    )
                else:
                    u = result.usage
                    content = result.content
                    row.route, row.route_reason = "vision", decisions[n].reason
                    row.model, row.prompt_version = result.model, call.prompt_version
                    row.request_hash = call.request_hash
                    row.input_tokens, row.cached_input_tokens = u.input_tokens, u.cached_tokens
                    row.output_tokens = u.output_tokens
                    row.cost_usd, row.cached = call.cost_usd, call.cached
            elif decisions[n].route == "vision":
                row.route = "text"
                row.route_reason = f"{decisions[n].reason}; vision call failed, used text layer"
            else:
                row.route, row.route_reason = "text", decisions[n].reason
            row.title = content.title
            row.content_json = content.model_dump(mode="json")
            rows.append(row)
        # Keepers before the pages pointing at them, for the self-referencing FK.
        db.add_all(sorted(rows, key=lambda r: r.duplicate_of is not None))
        db.flush()
        db.flush()

    # Status

    def _set_status(self, file_id: uuid.UUID, status: FileStatus, detail: str | None) -> None:
        with self.session_factory() as db:
            file = db.get(File, file_id)
            if file is not None:
                file.status, file.status_detail = status.value, detail
                db.commit()

    def _progress(self, file_id: uuid.UUID, done: int, total: int) -> None:
        with self.session_factory() as db:
            file = db.get(File, file_id)
            if file is not None and file.status == FileStatus.PARSING.value:
                file.status_detail = f"Reading diagrams with the model: {done}/{total} pages"
                db.commit()


def _build_captions(
    page: PageInfo, by_no: dict[int, PageInfo], duplicates: dict[int, Duplicate]
) -> list[str]:
    """Text from earlier build steps of `page` that the final page no longer shows."""
    keep = {line.lower() for line in page.body_lines}
    captions = []
    for n in sorted(by_no):
        dup = duplicates.get(n)
        if dup is None or dup.of != page.page_no or dup.reason.startswith("repeats"):
            continue
        title = (by_no[n].title or "").lower()
        new = [ln for ln in by_no[n].body_lines if ln.lower() not in keep and ln.lower() != title]
        if new:
            captions.append(" ".join(new))
    return captions


def _summary(pages, duplicates, calls: dict[int, PageCall]) -> str:
    vision = [c for c in calls.values() if c.result.content is not None]
    cached = sum(c.cached for c in vision)
    spent = sum(c.cost_usd or 0.0 for c in vision if not c.cached)
    text = len(pages) - len(vision) - len(duplicates)
    return (
        f"{len(pages)} pages: {len(vision)} vision ({cached} cached), {text} text, "
        f"{len(duplicates)} duplicates · this parse ${spent:.2f}"
    )
