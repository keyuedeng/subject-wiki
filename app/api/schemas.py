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
