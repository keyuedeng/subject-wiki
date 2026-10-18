"""subjects and files; enable pgvector

Revision ID: 0001
Revises:
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FILE_TYPES = (
    "slides",
    "transcript",
    "tutorial",
    "solutions",
    "past_paper",
    "outline",
    "reading",
    "other",
)
FILE_STATUSES = ("uploaded", "parsed", "failed")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    # Unused until phase 4 (embeddings); enabled now so later migrations can rely on it.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "subjects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("code", sa.String(32)),
        sa.Column("semester", sa.String(32)),
        sa.Column("weeks", sa.SmallInteger(), nullable=False, server_default="12"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("weeks BETWEEN 1 AND 52", name="ck_subjects_weeks_range"),
    )

    op.create_table(
        "files",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Uuid(),
            sa.ForeignKey("subjects.id", ondelete="CASCADE", name="fk_files_subject_id_subjects"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("ext", sa.String(16), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("week", sa.SmallInteger()),
        sa.Column("lecture", sa.SmallInteger()),
        sa.Column("has_timestamps", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("included", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("classify_confidence", sa.Float(), nullable=False),
        sa.Column("classify_reason", sa.Text(), nullable=False),
        sa.Column("reviewed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(16), nullable=False, server_default="uploaded"),
        sa.Column("status_detail", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("subject_id", "sha256", name="uq_files_subject_sha256"),
        sa.CheckConstraint(_in_list("type", FILE_TYPES), name="ck_files_type_valid"),
        sa.CheckConstraint(_in_list("status", FILE_STATUSES), name="ck_files_status_valid"),
        sa.CheckConstraint("week IS NULL OR week BETWEEN 0 AND 52", name="ck_files_week_range"),
        sa.CheckConstraint(
            "lecture IS NULL OR lecture BETWEEN 0 AND 99", name="ck_files_lecture_range"
        ),
        sa.CheckConstraint("classify_confidence BETWEEN 0 AND 1", name="ck_files_confidence_range"),
    )
    op.create_index("ix_files_subject_id", "files", ["subject_id"])


def downgrade() -> None:
    op.drop_index("ix_files_subject_id", table_name="files")
    op.drop_table("files")
    op.drop_table("subjects")
    # The vector extension is left in place; other schemas may use it.
