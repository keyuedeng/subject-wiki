"""chunks: the search index (pgvector embeddings + Postgres full-text search)

- embedding vector(1536) with an HNSW index (cosine)
- tsv: generated tsvector, title weighted A and body B, with a GIN index. Not mapped in
  app/db/models.py (SQLite in tests has no tsvector); used by app/search/backend.py.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Uuid(),
            sa.ForeignKey("subjects.id", ondelete="CASCADE", name="fk_chunks_subject_id_subjects"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey("files.id", ondelete="CASCADE", name="fk_chunks_file_id_files"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("meta", JSONB(), nullable=False, server_default="{}"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "tsv",
            sa.dialects.postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('english', title), 'A') || "
                "setweight(to_tsvector('english', body), 'B')",
                persisted=True,
            ),
        ),
        sa.CheckConstraint(
            "source_type IN ('slide', 'notes', 'transcript', 'tutorial')",
            name="ck_chunks_source_type_valid",
        ),
    )
    op.create_index("ix_chunks_subject_id", "chunks", ["subject_id"])
    op.create_index("ix_chunks_file_id", "chunks", ["file_id"])
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    op.execute(
        "CREATE INDEX ix_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_table("chunks")
