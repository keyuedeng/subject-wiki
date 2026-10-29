import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.classify.types import FileType


class SubjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=32)
    semester: str | None = Field(default=None, max_length=32)
    weeks: int = Field(default=12, ge=1, le=52)

    @field_validator("name", "code", "semester", mode="before")
    @classmethod
    def strip(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


class SubjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str | None
    semester: str | None
    weeks: int
    created_at: datetime


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject_id: uuid.UUID
    filename: str
    title: str | None
    storage_path: str
    sha256: str
    mime: str
    size_bytes: int
    type: FileType
    week: int | None
    lecture: int | None
    has_timestamps: bool
    included: bool
    classify_confidence: float
    classify_reason: str
    reviewed: bool
    needs_review: bool
    status: str
    status_detail: str | None
    page_count: int | None
    created_at: datetime


class FileUpdate(BaseModel):
    """Only the fields sent are changed; send week/lecture as null to clear them."""

    model_config = ConfigDict(extra="forbid")

    type: FileType | None = None
    week: int | None = Field(default=None, ge=0, le=52)
    lecture: int | None = Field(default=None, ge=0, le=99)
    included: bool | None = None
    reviewed: bool | None = None


class UploadOut(BaseModel):
    filename: str
    outcome: Literal["stored", "duplicate", "rejected"]
    message: str
    file: FileOut | None = None


class SlideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    page_no: int
    title: str | None
    route: Literal["text", "vision", "duplicate"]
    route_reason: str
    route_signals: dict
    content_json: dict
    notes: str | None
    duplicate_of: uuid.UUID | None
    image_path: str
    model: str | None
    prompt_version: str | None
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost_usd: float | None
    cached: bool


class ParseQueued(BaseModel):
    queued: int
    file_ids: list[uuid.UUID]


class SegmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    idx: int
    start_ms: int | None
    end_ms: int | None
    raw_text: str
    clean_text: str
    clean_status: str
    clean_note: str | None
    low_conf_words: list
    flags: list
    slide_id: uuid.UUID | None
    align_score: float | None
    align_confidence: float | None
    prompt_version: str | None
    cost_usd: float | None
    cached: bool


class PairingIn(BaseModel):
    slides_file_ids: list[uuid.UUID]


class PairingOut(BaseModel):
    slides_file_ids: list[uuid.UUID]
    source: str | None


ChunkType = Literal["slide", "notes", "transcript", "tutorial"]


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    weeks: list[int] = Field(default_factory=list)
    types: list[ChunkType] = Field(default_factory=list)
    k: int = Field(default=10, ge=1, le=50)
    mode: Literal["hybrid", "keyword", "embedding"] = "hybrid"


class SourceOut(BaseModel):
    chunk_id: uuid.UUID
    source_type: str
    label: str
    href: str
    file_id: uuid.UUID
    file_title: str
    week: int | None
    title: str
    body: str
    rank: int | None = None
    keyword_rank: int | None = None
    embedding_rank: int | None = None
    similarity: float | None = None


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)
    weeks: list[int] = Field(default_factory=list)
    types: list[ChunkType] = Field(default_factory=list)
    mode: Literal["auto", "answer", "where"] = "auto"


class AnswerOut(BaseModel):
    kind: Literal["answer"] = "answer"
    status: Literal["answered", "partial", "not_covered"]
    answer_markdown: str  # inline [S1] markers; `handles` maps them to sources
    handles: dict[str, uuid.UUID]
    cited_chunk_ids: list[uuid.UUID]
    missing: str | None
    warnings: list[str]
    sources: list[SourceOut]
    cost_usd: float
    cached: bool


class WhereItemOut(BaseModel):
    level: Literal["main", "mentioned"]
    label: str
    href: str
    chunk_ids: list[uuid.UUID]


class WhereWeekOut(BaseModel):
    week: int | None
    items: list[WhereItemOut]


class WhereOut(BaseModel):
    kind: Literal["where"] = "where"
    topic: str
    found: bool
    weeks: list[WhereWeekOut]
    missing_note: str | None
    sources: list[SourceOut]
    cost_usd: float
    cached: bool
