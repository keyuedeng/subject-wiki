import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.classify.types import LOW_CONFIDENCE, FileStatus, FileType
from app.db.base import Base

# Chunk ids are derived from the source they index, so re-indexing updates rows in place.
CHUNK_ID_NAMESPACE = uuid.UUID("0b8f4f7e-3c2d-4a59-9a1e-7d6c5b4a3f21")
# Fixed by the vector column (migration 0006); EMBEDDING_DIMENSIONS must match it.
CHUNK_DIMENSIONS = 1536

# Slide ids are derived, not random, so they survive re-parsing (and delete + re-upload
# of the same file in the same subject); transcript segments point at them.
SLIDE_ID_NAMESPACE = uuid.UUID("6f1c2a52-9d6e-4c1b-8f0e-2b7a5d0c9e11")


def slide_id(subject_id: uuid.UUID, sha256: str, page_no: int) -> uuid.UUID:
    return uuid.uuid5(SLIDE_ID_NAMESPACE, f"{subject_id}:{sha256}:{page_no}")


def chunk_id(subject_id: uuid.UUID, source_key: str) -> uuid.UUID:
    return uuid.uuid5(CHUNK_ID_NAMESPACE, f"{subject_id}:{source_key}")


def _in_list(column: str, values: type[FileType] | type[FileStatus]) -> str:
    return f"{column} IN ({', '.join(repr(v.value) for v in values)})"


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text)
    code: Mapped[str | None] = mapped_column(String(32))
    semester: Mapped[str | None] = mapped_column(String(32))
    weeks: Mapped[int] = mapped_column(SmallInteger, default=12)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    files: Mapped[list["File"]] = relationship(
        back_populates="subject", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (CheckConstraint("weeks BETWEEN 1 AND 52", name="weeks_range"),)


class File(Base):
    __tablename__ = "files"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(Text)
    # Topic read from the document's own large text at upload ("Decision Trees"), if any.
    title: Mapped[str | None] = mapped_column(Text)
    ext: Mapped[str] = mapped_column(String(16))
    storage_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    mime: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)

    type: Mapped[str] = mapped_column(String(16))
    week: Mapped[int | None] = mapped_column(SmallInteger)
    lecture: Mapped[int | None] = mapped_column(SmallInteger)
    has_timestamps: Mapped[bool] = mapped_column(default=False)
    included: Mapped[bool] = mapped_column(default=True)

    # The classifier's original guess; edits change the fields above, not these.
    classify_confidence: Mapped[float] = mapped_column(Float)
    classify_reason: Mapped[str] = mapped_column(Text)
    # True once a person has set or confirmed type/week/lecture.
    reviewed: Mapped[bool] = mapped_column(default=False)

    status: Mapped[str] = mapped_column(String(16), default=FileStatus.UPLOADED.value)
    status_detail: Mapped[str | None] = mapped_column(Text)
    # Which process is working on the file, until when (app/jobs/lease.py).
    lease_owner: Mapped[str | None] = mapped_column(String(80))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    page_count: Mapped[int | None] = mapped_column(SmallInteger)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    subject: Mapped[Subject] = relationship(back_populates="files")
    slides: Mapped[list["Slide"]] = relationship(
        back_populates="file",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Slide.page_no",
    )

    __table_args__ = (
        UniqueConstraint("subject_id", "sha256", name="uq_files_subject_sha256"),
        CheckConstraint(_in_list("type", FileType), name="type_valid"),
        CheckConstraint(_in_list("status", FileStatus), name="status_valid"),
        CheckConstraint("week IS NULL OR week BETWEEN 0 AND 52", name="week_range"),
        CheckConstraint("lecture IS NULL OR lecture BETWEEN 0 AND 99", name="lecture_range"),
        CheckConstraint("classify_confidence BETWEEN 0 AND 1", name="confidence_range"),
    )

    @property
    def needs_review(self) -> bool:
        return not self.reviewed and self.classify_confidence < LOW_CONFIDENCE


# JSONB on Postgres, plain JSON elsewhere (tests run on SQLite).
JsonType = JSON().with_variant(JSONB(), "postgresql")

ROUTES = ("text", "vision", "duplicate")


class Slide(Base):
    """One parsed page of a slides file."""

    __tablename__ = "slides"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    page_no: Mapped[int] = mapped_column(SmallInteger)
    title: Mapped[str | None] = mapped_column(Text)
    text_raw: Mapped[str] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)  # PPTX speaker notes
    content_json: Mapped[dict] = mapped_column(JsonType)
    route: Mapped[str] = mapped_column(String(16))
    route_reason: Mapped[str] = mapped_column(Text)
    route_signals: Mapped[dict] = mapped_column(JsonType)
    image_path: Mapped[str] = mapped_column(Text)
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("slides.id", ondelete="SET NULL")
    )
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    request_hash: Mapped[str | None] = mapped_column(String(64))  # -> llm_cache
    input_tokens: Mapped[int] = mapped_column(default=0)
    cached_input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    # Tokens and cost of the call that produced content_json (0 on the text path).
    # cached=True means this parse reused that result and was not charged again.
    cost_usd: Mapped[float | None] = mapped_column(Float)
    cached: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    file: Mapped[File] = relationship(back_populates="slides")

    __table_args__ = (
        UniqueConstraint("file_id", "page_no", name="uq_slides_file_page"),
        CheckConstraint(f"route IN ({', '.join(repr(r) for r in ROUTES)})", name="route_valid"),
    )


class LlmCall(Base):
    """One paid model call (LLM or embedding), keyed by a hash of its full request.
    See app/llm/cache.py. Survives file deletes and is shared across subjects."""

    __tablename__ = "llm_cache"

    request_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # e.g. slide_vision, transcript_clean
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))  # reporting only
    response_json: Mapped[dict] = mapped_column(JsonType)
    input_tokens: Mapped[int] = mapped_column(default=0)
    cached_input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)  # what the original call cost
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


TRANSCRIPT_FLAGS = ("exam_hint", "worked_example", "analogy", "student_question", "admin")


class TranscriptDeck(Base):
    """Which slides file(s) a transcript belongs to, in order (a lecture can span two
    decks). `source` is "auto" (by week/lecture number) or "manual" (set in the UI;
    never replaced by auto-pairing)."""

    __tablename__ = "transcript_decks"

    transcript_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    slides_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(SmallInteger, default=0)
    source: Mapped[str] = mapped_column(String(8), default="auto")

    __table_args__ = (CheckConstraint("source IN ('auto', 'manual')", name="source_valid"),)


class PairingSuggestion(Base):
    """For a transcript that couldn't be paired automatically (several decks in its week
    and no lecture number to tell them apart): the deck whose slide text best matches what
    was said. Shown for the user to confirm; never used for processing until confirmed.
    `candidates` is the set of decks it was chosen from, so it's recomputed when that
    set changes."""

    __tablename__ = "pairing_suggestions"

    transcript_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    slides_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    score: Mapped[float] = mapped_column(Float)  # cosine similarity, 0-1
    runner_up: Mapped[float | None] = mapped_column(Float)  # the next deck's score
    candidates: Mapped[list] = mapped_column(JsonType)  # ["<deck id>:<status>", ...]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TranscriptSegment(Base):
    """One window (~30-60 s) of a transcript: raw and cleaned text, flags, and the
    slide it was aligned to (null = not matched to any slide)."""

    __tablename__ = "transcript_segments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(SmallInteger)
    start_ms: Mapped[int | None]
    end_ms: Mapped[int | None]
    raw_text: Mapped[str] = mapped_column(Text)
    clean_text: Mapped[str] = mapped_column(Text)
    clean_status: Mapped[str] = mapped_column(String(16))  # cleaned / rejected / failed
    clean_note: Mapped[str | None] = mapped_column(Text)  # why a clean was rejected
    low_conf_words: Mapped[list] = mapped_column(JsonType, default=list)
    flags: Mapped[list] = mapped_column(JsonType, default=list)  # [{type, reason, quote}]

    slide_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("slides.id", ondelete="SET NULL"), index=True
    )
    align_score: Mapped[float | None] = mapped_column(Float)  # cosine similarity
    align_confidence: Mapped[float | None] = mapped_column(Float)  # posterior probability

    model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(default=0)
    cached_input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    cached: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    slide: Mapped["Slide | None"] = relationship()

    __table_args__ = (
        UniqueConstraint("file_id", "idx", name="uq_transcript_segments_file_idx"),
        CheckConstraint(
            "clean_status IN ('cleaned', 'rejected', 'failed')", name="clean_status_valid"
        ),
    )


CHUNK_TYPES = ("slide", "notes", "transcript", "tutorial")


class Chunk(Base):
    """One searchable unit of a subject: a kept slide, a slide's speaker notes, a
    transcript segment or a tutorial section. Week, type and `included` are read from the
    file at query time, so edits on the board apply to search without re-indexing.

    Postgres also has a generated `tsv` column (title weighted A, body B) with a GIN
    index, used by keyword search (app/search/backend.py). It isn't mapped here, so the
    tests' SQLite schema doesn't need it."""

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # chunk_id(subject, key)
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[str] = mapped_column(String(16))
    source_key: Mapped[str] = mapped_column(Text)  # e.g. "slide:<slide id>"
    position: Mapped[int]  # page number, or segment index: order within the file
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column(JsonType)  # page_no, start_ms, slide_id, flags, ...
    # sha256 of the embedded text and embedding model: unchanged chunks aren't re-embedded.
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding: Mapped[list] = mapped_column(
        JSON().with_variant(Vector(CHUNK_DIMENSIONS), "postgresql")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"source_type IN ({', '.join(repr(t) for t in CHUNK_TYPES)})", name="source_type_valid"
        ),
    )


MESSAGE_ROLES = ("user", "assistant")
MESSAGE_KINDS = ("question", "answer", "where", "error")


class Chat(Base):
    """One Ask conversation in a subject, titled from its first question."""

    __tablename__ = "chats"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subject_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan", order_by="ChatMessage.idx"
    )


class ChatMessage(Base):
    """A question, or the answer to it. An answer's `payload` is a snapshot of what was
    shown (sources with their labels, citations, follow-ups), so an old chat renders the
    same even after files are re-indexed or deleted."""

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(SmallInteger)
    role: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)  # the question, or the answer's markdown
    mode: Mapped[str | None] = mapped_column(String(16))  # the question's mode
    filters: Mapped[dict] = mapped_column(JsonType, default=dict)  # {weeks, types}
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cached: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chat: Mapped[Chat] = relationship(back_populates="messages")

    __table_args__ = (
        UniqueConstraint("chat_id", "idx", name="uq_chat_messages_chat_idx"),
        CheckConstraint(
            f"role IN ({', '.join(repr(r) for r in MESSAGE_ROLES)})", name="role_valid"
        ),
        CheckConstraint(
            f"kind IN ({', '.join(repr(k) for k in MESSAGE_KINDS)})", name="kind_valid"
        ),
    )
