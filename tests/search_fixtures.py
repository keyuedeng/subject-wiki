"""In-memory search backend, a fake Q&A model, and a small indexed subject for search and
Q&A tests. No network, no Postgres: `MemoryBackend` gives the same statistics PgBackend
reads from tsvectors and pgvector (crude stemming instead of Postgres's English one)."""

import hashlib
import re
import threading
import uuid

import numpy as np
from sqlalchemy import select

from app.classify.types import FileType
from app.db.models import Chunk, File, Slide, TranscriptSegment, slide_id
from app.llm.cache import LlmCache
from app.llm.client import LlmClient, StructuredResponse, Usage
from app.search.backend import Filters
from app.search.index import INDEXED_TYPES, Indexer
from app.search.rank import TermStats
from tests.transcript_fixtures import bag_of_words

STOP = {
    "a", "an", "the", "is", "are", "was", "of", "to", "in", "on", "for", "and", "or", "it",
    "what", "who", "how", "why", "does", "do", "should", "which", "that", "this", "with", "by",
    "be", "as", "at", "its", "according", "where", "taught", "covered", "week",
}  # fmt: skip


def stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def terms(text: str) -> list[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP]


class MemoryBackend:
    """Same interface as app.search.backend.PgBackend, over the ORM rows."""

    def _rows(self, db, subject_id, filters: Filters) -> list[Chunk]:
        q = (
            select(Chunk)
            .join(File, File.id == Chunk.file_id)
            .where(Chunk.subject_id == subject_id, File.included, File.type.in_(INDEXED_TYPES))
        )
        if filters.weeks:
            q = q.where(File.week.in_(filters.weeks))
        if filters.types:
            q = q.where(Chunk.source_type.in_(filters.types))
        return list(db.scalars(q))

    def query_terms(self, db, query: str) -> list[str]:
        return list(dict.fromkeys(terms(query)))

    def term_stats(self, db, subject_id, query_terms, filters) -> TermStats:
        rows = self._rows(db, subject_id, filters)
        lengths = []
        stats = TermStats(len(rows), 0.0)
        for c in rows:
            counts: dict[str, int] = {}
            for t in terms(c.title):
                counts[t] = counts.get(t, 0) + 2  # title weighted, like tsv weight A
            for t in terms(c.body):
                counts[t] = counts.get(t, 0) + 1
            lengths.append(len(counts))
            hit = {t: n for t, n in counts.items() if t in query_terms}
            if hit:
                stats.postings[c.id] = (len(counts), hit)
        stats.avg_len = sum(lengths) / len(lengths) if lengths else 0.0
        return stats

    def nearest(self, db, subject_id, vector, filters, limit):
        q = np.array(vector)
        out = []
        for c in self._rows(db, subject_id, filters):
            v = np.array(c.embedding)
            denom = np.linalg.norm(q) * np.linalg.norm(v)
            out.append((c.id, float(q @ v / denom) if denom else 0.0))
        return sorted(out, key=lambda x: -x[1])[:limit]

    def phrase_matches(self, db, subject_id, phrase, filters, limit):
        want = terms(phrase)
        out = []
        for c in self._rows(db, subject_id, filters):
            body = terms(c.title + " " + c.body)
            if want and any(body[i : i + len(want)] == want for i in range(len(body))):
                out.append(c.id)
        return out[:limit]


class FakeQAProvider:
    """Embeds by hashed word counts; answers structured calls with `answer` / `where`
    callables that get the request text and return the response dict."""

    name = "fake"

    def __init__(self, answer=None, where=None):
        self.answer = answer or (lambda text: {"status": "answered",
                                               "answer_markdown": "See [S1].", "cited": ["S1"],
                                               "missing": None})  # fmt: skip
        self.where = where or (lambda text: {"items": []})
        self.structured_calls: list[str] = []
        self.embedded: list[str] = []
        self._lock = threading.Lock()

    def structured(self, *, model, instructions, content, schema, effort, max_output_tokens,
                   cache_hint=None):  # fmt: skip
        text = content[-1]["text"]
        with self._lock:
            self.structured_calls.append(text)
        return StructuredResponse(schema.model_validate(self.respond(schema, text)),
                                  Usage(2000, 0, 500, 0))  # fmt: skip

    def respond(self, schema, text: str) -> dict:
        """The canned response, with fields a test didn't care about filled in."""
        if schema.__name__ == "WhereCoverage":
            return {"summary": "", "follow_ups": [], **self.where(text)}
        return {"topic": "", "follow_ups": [], **self.answer(text)}

    def embed(self, *, model, texts, dimensions):
        with self._lock:
            self.embedded.extend(texts)
        return [bag_of_words(t) for t in texts], 10 * len(texts)


class StreamingQAProvider(FakeQAProvider):
    """Also streams: sends the response's JSON text in small pieces before returning."""

    def __init__(self, *args, piece: int = 7, **kwargs):
        super().__init__(*args, **kwargs)
        self.piece = piece
        self.streamed: list[str] = []

    def structured_stream(self, *, on_text, model, instructions, content, schema, effort,
                          max_output_tokens, cache_hint=None):  # fmt: skip
        text = content[-1]["text"]
        with self._lock:
            self.structured_calls.append(text)
        parsed = schema.model_validate(self.respond(schema, text))
        raw = parsed.model_dump_json()
        for i in range(0, len(raw), self.piece):
            self.streamed.append(raw[i : i + self.piece])
            on_text(raw[i : i + self.piece])
        return StructuredResponse(parsed, Usage(2000, 0, 500, 0))


def make_llm(session_factory, provider) -> LlmClient:
    return LlmClient(lambda: provider, LlmCache(session_factory))


def make_indexer(session_factory, provider) -> Indexer:
    return Indexer(session_factory, make_llm(session_factory, provider), "text-embedding-3-large",
                   256)  # fmt: skip


def _file(db, subject, filename, ftype, week, lecture) -> File:
    sha = hashlib.sha256(f"{subject.id}:{filename}".encode()).hexdigest()
    ext = filename.rsplit(".", 1)[-1]
    f = File(
        subject_id=subject.id, filename=filename, ext=ext, storage_path=f"{subject.id}/{sha}.{ext}",
        sha256=sha, mime="application/octet-stream", size_bytes=1000, type=ftype.value,
        week=week, lecture=lecture, classify_confidence=0.9, classify_reason="test",
        reviewed=True, status="parsed",
    )  # fmt: skip
    db.add(f)
    db.flush()
    return f


def add_deck(db, subject, filename, week, lecture, pages, notes=None, duplicates=None) -> File:
    """`pages`: (title, markdown) per page. `duplicates`: page -> page it collapses into."""
    f = _file(db, subject, filename, FileType.SLIDES, week, lecture)
    notes, duplicates = notes or {}, duplicates or {}
    for no, (title, md) in enumerate(pages, 1):
        sid = slide_id(subject.id, f.sha256, no)
        dup = slide_id(subject.id, f.sha256, duplicates[no]) if no in duplicates else None
        db.add(Slide(
            id=sid, file_id=f.id, page_no=no, title=title, text_raw=md, notes=notes.get(no),
            content_json={"title": title, "content_markdown": f"# {title}\n\n{md}",
                          "visuals": [], "equations": [], "code_blocks": []},
            route="duplicate" if dup else "text", route_reason="test", route_signals={},
            image_path=f"pages/{f.sha256}/{no}.png", duplicate_of=dup,
        ))  # fmt: skip
    db.flush()
    return f


def add_transcript(db, subject, filename, week, lecture, deck, windows) -> File:
    """`windows`: (text, page shown or None, flag types) per 45 s window."""
    f = _file(db, subject, filename, FileType.TRANSCRIPT, week, lecture)
    f.has_timestamps = True
    for i, (text, page, flags) in enumerate(windows):
        db.add(TranscriptSegment(
            id=uuid.uuid4(), file_id=f.id, idx=i, start_ms=i * 45_000, end_ms=(i + 1) * 45_000,
            raw_text=text, clean_text=text, clean_status="cleaned",
            flags=[{"type": t, "reason": "test", "quote": text[:20]} for t in flags],
            slide_id=slide_id(subject.id, deck.sha256, page) if page else None,
        ))  # fmt: skip
    db.flush()
    return f


GRASP = [
    (
        "GRASP: Creator",
        "Assign class B the responsibility to create instances of class A if B "
        "contains or compositely aggregates A, records A, closely uses A, or has the "
        "initializing data for A.",
    ),
    (
        "GRASP: Information Expert",
        "Assign a responsibility to the information expert: the "
        "class that has the information necessary to fulfil the responsibility.",
    ),
    (
        "GRASP: Information Expert",
        "Assign a responsibility to the information expert: the "
        "class that has the information necessary to fulfil the responsibility. Example: Sale "
        "knows its total.",
    ),  # build step of page 2
    (
        "GRASP: Controller",
        "Assign the responsibility for handling a system event message to a "
        "controller class representing the overall system or a use case scenario.",
    ),
    (
        "GRASP: Low Coupling",
        "Assign responsibilities so that coupling remains low: fewer "
        "dependencies between classes means less impact when a class changes.",
    ),
]
STATE_MACHINES = [
    (
        "Transition Actions and Guards",
        "A guard is a boolean condition in square brackets; if "
        "it is false the transition is not taken. The action after the slash runs on the "
        "transition.",
    ),
    (
        "Choice Pseudostate",
        "A choice pseudostate splits a transition into branches, each with "
        "its own guard condition.",
    ),
]
LECTURE_4 = [
    (
        "Welcome back everyone, the assignment is due on Friday and the tutorial rooms changed.",
        None,
        ["admin"],
    ),
    (
        "So Creator says the container that aggregates the parts should create them, and this "
        "will be on the exam.",
        1,
        ["exam_hint"],
    ),
    (
        "Creator again: the board aggregates squares, so the board creates the squares in the "
        "Monopoly example.",
        1,
        ["worked_example"],
    ),
    (
        "Information expert: give the responsibility to the class with the information, so the "
        "sale computes its own total.",
        3,
        [],
    ),
]


def build_subject(db, subject):
    """Week 4: GRASP deck (with a build step and lecturer notes) and its transcript;
    week 6: state machines deck. Returns (grasp, transcript, states)."""
    grasp = add_deck(db, subject, "Lecture 4 GRASP.pdf", 4, 4, GRASP,
                     notes={4: "Remind them the controller is not a UI class."},
                     duplicates={2: 3})  # fmt: skip
    vtt = add_transcript(db, subject, "Lecture 4.vtt", 4, 4, grasp, LECTURE_4)
    states = add_deck(db, subject, "Lecture 6 State Machines.pdf", 6, 6, STATE_MACHINES)
    db.commit()
    return grasp, vtt, states
