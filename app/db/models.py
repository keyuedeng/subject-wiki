import uuid
from datetime import datetime

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.classify.types import LOW_CONFIDENCE, FileStatus, FileType
from app.db.base import Base


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

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    subject: Mapped[Subject] = relationship(back_populates="files")

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
