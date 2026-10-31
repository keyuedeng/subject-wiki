"""Process one tutorial or solutions sheet: read -> blocks -> sections -> labels -> rows.

- PDF and DOCX (converted with LibreOffice) go through the slide pipeline's document
  reader: the measured text layer, plus cached vision calls for pages with diagrams or
  images, whose descriptions become figure blocks. Notebooks are read directly.
- Tutorials are labelled (app/tutorials/label.py): one call for the whole sheet, then a
  lecture search per concept and one call per section. Every call goes through the
  request-hash cache, so processing again costs nothing unless the sheet or the
  lectures' text changed. Solutions sheets are split but not labelled.
- Afterwards exercises are linked to their solutions (app/tutorials/solutions.py).
"""

import logging
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.classify.titles import display_title
from app.classify.types import FileStatus, FileType
from app.db.models import File, Subject, TutorialSection, tutorial_section_id
from app.jobs.lease import Leases, held
from app.llm.client import LlmClient
from app.parse.pipeline import Parser
from app.qa.labels import SourceRef
from app.storage.client import Storage
from app.tutorials import label as lb
from app.tutorials.blocks import ipynb_blocks, pdf_blocks
from app.tutorials.sections import SectionSpec, split_sections
from app.tutorials.solutions import link_solutions

logger = logging.getLogger(__name__)

TUTORIAL_EXTS = {"pdf", "docx", "ipynb"}
TUTORIAL_TYPES = {FileType.TUTORIAL.value, FileType.SOLUTIONS.value}

# (db, subject, query) -> lecture sources for a concept, best first.
LectureSearch = Callable[[Session, Subject, str], list[SourceRef]]


class TutorialError(Exception):
    pass


def can_process_tutorial(file: File) -> bool:
    return file.type in TUTORIAL_TYPES and file.ext in TUTORIAL_EXTS


@dataclass
class TutorialSettings:
    model: str
    effort: str
    concurrency: int = 4


@dataclass
class TutorialProcessor:
    session_factory: Callable[[], Session]
    storage: Storage
    llm: LlmClient
    parser: Parser
    search: LectureSearch
    settings: TutorialSettings
    leases: Leases | None = None
    # Called after a successful run (the queue indexes the subject's tutorials).
    on_processed: Callable[[uuid.UUID], None] | None = None

    def process(self, file_id: uuid.UUID) -> None:
        """Run and record the outcome on the file; never raises."""
        with held(self.leases, file_id, "tutorial processing") as ok:
            if not ok:
                return
            try:
                summary = self._process(file_id)
            except Exception as exc:
                logger.exception("tutorial processing failed for %s", file_id)
                message = (
                    str(exc) if isinstance(exc, TutorialError) else (f"{type(exc).__name__}: {exc}")
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
                raise TutorialError("File no longer exists")
            if not can_process_tutorial(file):
                raise TutorialError("Only tutorial and solutions sheets (PDF, DOCX, .ipynb)")
            sha, ext, path, ftype = file.sha256, file.ext, file.storage_path, file.type
            subject_id, title = file.subject_id, _title(file)

        data = self.storage.get(path)
        spent, notes, pages = 0.0, [], None
        if ext == "ipynb":
            try:
                blocks = ipynb_blocks(data)
            except ValueError as exc:
                raise TutorialError(f"Not a readable notebook ({exc})") from exc
        else:
            self._progress(file_id, "Reading the sheet")
            deck, calls, errors = self.parser.read_document(file_id, sha, ext, data)
            pages = len(deck.pages)
            figures = {n: _figures(c) for n, c in calls.items() if c.result.content is not None}
            spent += sum(c.cost_usd or 0 for c in calls.values() if not c.cached)
            if errors:
                notes.append(
                    f"{len(errors)} figure page(s) not described ({next(iter(errors.values()))})"
                )
            blocks = pdf_blocks(deck, figures)

        specs = split_sections(blocks)
        if not specs:
            raise TutorialError("No text found in the sheet")
        self._write_sections(file_id, subject_id, sha, specs, pages)

        labelled = 0
        if ftype == FileType.TUTORIAL.value:
            labelled, cost = self._label(file_id, subject_id, title)
            spent += cost
        with self.session_factory() as db:
            link_solutions(db, subject_id)
            db.commit()
        return _summary(specs, ftype, labelled, spent, notes)

    # Rows

    def _write_sections(self, file_id, subject_id, sha, specs: list[SectionSpec], pages) -> None:
        with self.session_factory() as db:
            existing = {s.idx: s for s in db.scalars(
                select(TutorialSection).where(TutorialSection.file_id == file_id))}  # fmt: skip
            for idx, spec in enumerate(specs):
                row = existing.get(idx) or TutorialSection(
                    id=tutorial_section_id(subject_id, sha, idx), file_id=file_id, idx=idx
                )
                start, end = spec.pages
                row.number, row.is_exercise = spec.number, spec.is_exercise
                row.label, row.title, row.text_md = spec.label, spec.title, spec.markdown()
                row.words, row.page_start, row.page_end = spec.words, start, end
                db.add(row)
            db.execute(delete(TutorialSection).where(
                TutorialSection.file_id == file_id, TutorialSection.idx >= len(specs)))  # fmt: skip
            file = db.get(File, file_id)
            if pages is not None:
                file.page_count = pages
            db.commit()

    # Labels

    def _label(self, file_id: uuid.UUID, subject_id: uuid.UUID, title: str) -> tuple[int, float]:
        cfg = self.settings
        with self.session_factory() as db:
            rows = list(db.scalars(select(TutorialSection).where(
                TutorialSection.file_id == file_id).order_by(TutorialSection.idx)))  # fmt: skip
            sids = lb.ids("T", len(rows))
            by_sid = dict(zip(sids, rows, strict=True))
            payload = [(sid, r.title, r.text_md) for sid, r in by_sid.items()]

        self._progress(file_id, "Labelling sections")
        first = self.llm.structured(
            kind="tutorial_sections",
            prompt_version=lb.SECTIONS_PROMPT_VERSION,
            model=cfg.model,
            instructions=lb.SECTIONS_PROMPT,
            input_text=lb.sections_request(title, payload),
            schema=lb.sections_schema(sids),
            effort=cfg.effort,
            max_output_tokens=lb.MAX_OUTPUT_TOKENS,
        )
        spent = 0.0 if first.cached else (first.call.cost_usd or 0.0)
        labels = lb.read_sections(first.response, sids)

        # Lecture evidence per concept (database work stays on this thread).
        self._progress(file_id, "Finding the lectures each section relies on")
        plans: dict[str, list[tuple[str, str, list[tuple[str, SourceRef]]]]] = {}
        with self.session_factory() as db:
            subject = db.get(Subject, subject_id)
            for sid, lab in labels.items():
                if not lab.concepts:
                    continue
                eids: dict[uuid.UUID, str] = {}
                concepts = []
                for n, name in enumerate(lab.concepts, 1):
                    found = []
                    for ref in self.search(db, subject, name)[: lb.EVIDENCE_PER_CONCEPT]:
                        eid = eids.setdefault(ref.chunk_id, f"E{len(eids) + 1}")
                        found.append((eid, ref))
                    concepts.append((f"C{n}", name, found))
                plans[sid] = concepts

        requests = {
            sid: self.llm.structured_request(
                kind="tutorial_coverage",
                prompt_version=lb.COVERAGE_PROMPT_VERSION,
                model=cfg.model,
                instructions=lb.COVERAGE_PROMPT,
                input_text=lb.coverage_request(by_sid[sid].title, by_sid[sid].text_md, plan),
                schema=lb.coverage_schema(
                    [cid for cid, _, _ in plan],
                    sorted(
                        {eid for _, _, found in plan for eid, _ in found}, key=lambda e: int(e[1:])
                    ),
                ),  # fmt: skip
                effort=cfg.effort,
                max_output_tokens=lb.MAX_OUTPUT_TOKENS,
            )
            for sid, plan in plans.items()
        }
        hits = self.llm.cache.get_many(r.key for r in requests.values())
        results: dict[str, tuple[dict, float, bool]] = {}
        for sid, req in requests.items():
            if hit := hits.get(req.key):
                results[sid] = (hit.response, 0.0, True)
        todo = [sid for sid in requests if sid not in results]
        failed: list[str] = []
        if todo:
            with ThreadPoolExecutor(max_workers=cfg.concurrency) as pool:
                futures = {pool.submit(self.llm.call, requests[sid]): sid for sid in todo}
                for done, future in enumerate(as_completed(futures), 1):
                    sid = futures[future]
                    try:
                        call = future.result()
                    except Exception as exc:
                        logger.warning("coverage call failed for %s: %s", sid, exc)
                        failed.append(f"{by_sid[sid].label}: {type(exc).__name__}: {exc}"[:200])
                        continue
                    self.llm.store(requests[sid], call)
                    results[sid] = (call.response, call.cost_usd or 0.0, False)
                    self._progress(file_id, f"Labelling sections: {done}/{len(todo)}")
        spent += sum(cost for _, cost, cached in results.values() if not cached)

        with self.session_factory() as db:
            for sid, row in by_sid.items():
                row = db.get(TutorialSection, row.id)
                lab = labels.get(sid)
                row.section_type = lab.type if lab else None
                row.prompt_version = lb.COVERAGE_PROMPT_VERSION
                if sid in results:
                    response, cost, cached = results[sid]
                    row.concepts = lb.read_coverage(response, plans[sid])
                    row.cost_usd, row.cached = cost, cached
                else:
                    row.concepts, row.cost_usd, row.cached = [], None, False
                row.coverage, row.earliest_week = lb.section_coverage(row.concepts)
            db.commit()
        if failed:
            raise TutorialError(
                f"{len(failed)} section(s) couldn't be labelled (the rest are saved; process "
                f"again to retry). First error: {failed[0]}"
            )
        return len(labels), spent

    # Status

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


def _title(file: File) -> str:
    return display_title(file.title, file.filename, FileType(file.type), file.week, file.lecture,
                         file.subject.code)  # fmt: skip


def _figures(call) -> list[str]:
    out = []
    for v in call.result.content.visuals:
        if not v.description.strip():
            continue
        kind = v.kind.value.replace("_", " ") if hasattr(v.kind, "value") else str(v.kind)
        text = f"Figure ({kind}): {v.description.strip()}"
        if v.key_facts:
            text += "\n" + "\n".join(f"- {fact}" for fact in v.key_facts)
        out.append(text)
    return out


def _summary(specs, ftype, labelled, spent, notes) -> str:
    exercises = sum(s.is_exercise for s in specs)
    parts = [f"{len(specs)} sections" + (f" ({exercises} exercises)" if exercises else "")]
    if ftype == FileType.TUTORIAL.value:
        parts.append(f"{labelled} labelled")
    parts += notes
    return " · ".join(parts) + f" · this run ${spent:.2f}"
