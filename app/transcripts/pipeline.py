"""Process one transcript: parse -> windows -> clean + flag -> embed -> align -> rows.

Every model call (cleaning, embeddings) goes through the request-hash cache, so running
again on unchanged inputs makes no API calls; changing only the alignment settings or
re-parsing a deck re-aligns for free (slide embeddings are cached by their text).
"""

import logging
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.classify.types import FileStatus
from app.db.models import File, Slide, TranscriptSegment
from app.jobs.lease import Leases, held
from app.llm.client import Cached, LlmClient
from app.storage.client import Storage
from app.transcripts.align import AlignParams, align, cosine_matrix
from app.transcripts.clean import (
    CLEAN_PROMPT,
    CLEAN_PROMPT_VERSION,
    MAX_OUTPUT_TOKENS,
    Checked,
    CleanedWindow,
    check,
    request_text,
)
from app.transcripts.formats import parse_transcript
from app.transcripts.glossary import build_glossary
from app.transcripts.pairing import decks_for, sync_auto_pairs
from app.transcripts.windows import Window, make_windows

logger = logging.getLogger(__name__)

TRANSCRIPT_EXTS = {"vtt", "srt", "txt"}
SLIDE_TEXT_CHARS = 6000  # cap per slide for embedding


class TranscriptError(Exception):
    pass


@dataclass
class TranscriptSettings:
    clean_model: str
    clean_effort: str
    embed_model: str
    embed_dimensions: int
    concurrency: int = 4
    align: AlignParams = AlignParams()


def slide_embedding_text(slide: Slide) -> str:
    """Title, content, visual descriptions and key facts, and speaker notes."""
    content = slide.content_json or {}
    parts = [slide.title or "", content.get("content_markdown") or ""]
    for visual in content.get("visuals") or []:
        parts.append(visual.get("description") or "")
        parts.extend(visual.get("key_facts") or [])
    if slide.notes:
        parts.append("Speaker notes: " + slide.notes)
    return "\n".join(p for p in parts if p)[:SLIDE_TEXT_CHARS]


@dataclass
class WindowResult:
    window: Window
    checked: Checked
    cached: Cached | None  # None when the call failed
    error: str | None = None


@dataclass
class TranscriptProcessor:
    session_factory: Callable[[], Session]
    storage: Storage
    llm: LlmClient
    settings: TranscriptSettings
    # Parses a slides file synchronously (decks are parsed first if needed).
    parse_slides: Callable[[uuid.UUID], None] | None = None
    # Called after a successful run (e.g. to index the segments for search).
    on_processed: Callable[[uuid.UUID], None] | None = None
    # Set by the queue; skips the run if another process is working on the file.
    leases: Leases | None = None

    def process(self, file_id: uuid.UUID) -> None:
        """Run and record the outcome on the file; never raises."""
        with held(self.leases, file_id, "processing") as ok:
            if ok:
                self._run(file_id)

    def _run(self, file_id: uuid.UUID) -> None:
        try:
            summary = self._process(file_id)
        except Exception as exc:
            logger.exception("transcript processing failed for %s", file_id)
            message = (
                str(exc) if isinstance(exc, TranscriptError) else (f"{type(exc).__name__}: {exc}")
            )
            self._set_status(file_id, FileStatus.FAILED, message[:500])
        else:
            self._set_status(file_id, FileStatus.PARSED, summary)
            if self.on_processed is not None:
                self.on_processed(file_id)

    def _process(self, file_id: uuid.UUID) -> str:
        with self.session_factory() as db:
            file = db.get(File, file_id)
            if file is None:
                raise TranscriptError("File no longer exists")
            sync_auto_pairs(db, file.subject_id)
            decks = [f for f, _ in decks_for(db, file_id)]
            if not decks:
                raise TranscriptError(
                    "Pair this transcript with its slides first (no deck has the same "
                    "lecture number)"
                )
            path, ext, code = file.storage_path, file.ext, file.subject.code
            unparsed = [d.id for d in decks if d.status != FileStatus.PARSED.value]

        for deck_id in unparsed:  # alignment needs the decks' parsed slides
            self._progress(file_id, "Parsing the paired slides first")
            if self.parse_slides is None:
                raise TranscriptError("Parse the paired slides first")
            self.parse_slides(deck_id)

        with self.session_factory() as db:
            slides = self._kept_slides(db, [d.id for d in decks])
            if not slides:
                raise TranscriptError("The paired slides have no parsed pages; parse them first")
            glossary = build_glossary(
                [{"title": s.title, "content_json": s.content_json, "notes": s.notes}
                 for s in self._all_slides(db, [d.id for d in decks])],
                code,
            )  # fmt: skip
            slide_ids = [s.id for s in slides]
            slide_texts = [slide_embedding_text(s) for s in slides]

        transcript = parse_transcript(self.storage.get(path), ext)
        windows = make_windows(transcript)
        if not windows:
            raise TranscriptError("The transcript has no text")

        results = self._clean_all(file_id, windows, glossary)

        self._progress(file_id, "Aligning to slides")
        texts = [r.checked.clean_text for r in results]
        cfg = self.settings
        vectors, embed_cost, embed_calls = self.llm.embed(
            model=cfg.embed_model, dimensions=cfg.embed_dimensions, texts=texts + slide_texts
        )
        sim = cosine_matrix(np.array(vectors[: len(texts)]), np.array(vectors[len(texts) :]))
        admin = [any(f["type"] == "admin" for f in r.checked.flags) for r in results]
        alignment = align(sim, [w.start_ms for w in windows], cfg.align, admin)

        with self.session_factory() as db:
            db.execute(delete(TranscriptSegment).where(TranscriptSegment.file_id == file_id))
            for r, a in zip(results, alignment, strict=True):
                w, c = r.window, r.cached
                call = c.call if c else None
                db.add(
                    TranscriptSegment(
                        file_id=file_id,
                        idx=w.idx,
                        start_ms=w.start_ms,
                        end_ms=w.end_ms,
                        raw_text=w.text,
                        clean_text=r.checked.clean_text,
                        clean_status=r.checked.status if c else "failed",
                        clean_note=r.error or r.checked.note,
                        low_conf_words=w.low_conf,
                        flags=r.checked.flags,
                        slide_id=slide_ids[a.slide] if a.slide is not None else None,
                        align_score=a.score,
                        align_confidence=a.confidence,
                        model=cfg.clean_model if c else None,
                        prompt_version=call.prompt_version if call else None,
                        request_hash=c.request_hash if c else None,
                        input_tokens=call.input_tokens if call else 0,
                        cached_input_tokens=call.cached_input_tokens if call else 0,
                        output_tokens=call.output_tokens if call else 0,
                        cost_usd=call.cost_usd if call else None,
                        cached=c.cached if c else False,
                    )
                )
            file = db.get(File, file_id)
            file.has_timestamps = transcript.has_timestamps
            db.commit()

        failed = [r for r in results if r.cached is None]
        if failed:
            raise TranscriptError(
                f"{len(failed)} of {len(results)} windows could not be cleaned (finished "
                f"windows are saved; process again to retry). First error: {failed[0].error}"
            )
        return _summary(results, alignment, embed_cost, embed_calls)

    def _clean_all(self, file_id, windows: list[Window], glossary: list[str]):
        """Cache lookups and writes happen here; worker threads only call the model."""
        cfg = self.settings
        requests = [
            self.llm.structured_request(
                kind="transcript_clean",
                prompt_version=CLEAN_PROMPT_VERSION,
                model=cfg.clean_model,
                instructions=CLEAN_PROMPT,
                input_text=request_text(
                    w.text, glossary, windows[i - 1].text.split(". ")[-1] if i else None,
                    w.low_conf,
                ),
                schema=CleanedWindow,
                effort=cfg.clean_effort,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            for i, w in enumerate(windows)
        ]  # fmt: skip
        hits = self.llm.cache.get_many(r.key for r in requests)
        out: list[WindowResult | None] = [None] * len(windows)

        def finish(i: int, call, cached: bool) -> None:
            w = windows[i]
            result = CleanedWindow.model_validate(call.response)
            out[i] = WindowResult(w, check(w.text, result, glossary),
                                  Cached(call.response, call, cached, requests[i].key))  # fmt: skip

        todo = []
        for i, req in enumerate(requests):
            if hit := hits.get(req.key):
                finish(i, hit, True)
            else:
                todo.append(i)

        if todo:
            self._progress(file_id, f"Cleaning: 0/{len(todo)} windows")
            with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
                futures = {pool.submit(self.llm.call, requests[i]): i for i in todo}
                for done, future in enumerate(as_completed(futures), 1):
                    i = futures[future]
                    try:
                        call = future.result()
                    except Exception as exc:
                        logger.warning("cleaning failed on window %s: %s", i, exc)
                        w = windows[i]
                        error = f"window {i}: {type(exc).__name__}: {exc}"[:300]
                        out[i] = WindowResult(w, Checked(w.text, "failed", None, []), None, error)
                        continue
                    self.llm.store(requests[i], call)
                    finish(i, call, False)
                    if done % 5 == 0 or done == len(todo):
                        self._progress(file_id, f"Cleaning: {done}/{len(todo)} windows")
        return out

    @staticmethod
    def _all_slides(db: Session, deck_ids: list[uuid.UUID]) -> list[Slide]:
        order = {d: i for i, d in enumerate(deck_ids)}
        rows = db.scalars(select(Slide).where(Slide.file_id.in_(deck_ids))).all()
        return sorted(rows, key=lambda s: (order[s.file_id], s.page_no))

    def _kept_slides(self, db: Session, deck_ids: list[uuid.UUID]) -> list[Slide]:
        """Pages that weren't collapsed into another page, in deck order."""
        return [s for s in self._all_slides(db, deck_ids) if s.duplicate_of is None]

    def _set_status(self, file_id: uuid.UUID, status: FileStatus, detail: str | None) -> None:
        with self.session_factory() as db:
            file = db.get(File, file_id)
            if file is not None:
                file.status, file.status_detail = status.value, detail
                db.commit()

    def _progress(self, file_id: uuid.UUID, detail: str) -> None:
        with self.session_factory() as db:
            file = db.get(File, file_id)
            if file is not None and file.status == FileStatus.PARSING.value:
                file.status_detail = detail
                db.commit()


def _summary(results: list[WindowResult], alignment, embed_cost: float, embed_calls: int) -> str:
    aligned = sum(a.slide is not None for a in alignment)
    flags = sum(len(r.checked.flags) for r in results)
    rejected = sum(r.checked.status == "rejected" for r in results)
    cached = sum(bool(r.cached and r.cached.cached) for r in results)
    spent = embed_cost + sum(
        (r.cached.call.cost_usd or 0.0) for r in results if r.cached and not r.cached.cached
    )
    parts = [
        f"{len(results)} windows ({cached} cached)",
        f"{aligned} aligned, {len(results) - aligned} unaligned",
        f"{flags} flags",
    ]
    if rejected:
        parts.append(f"{rejected} cleans rejected (raw text kept)")
    return " · ".join(parts) + f" · this run ${spent:.2f}"
