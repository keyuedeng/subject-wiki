"""transcripts: transcript_decks, transcript_segments; derived slide ids

Slide ids become uuid5(subject_id:sha256:page_no) so they survive re-parsing. Existing
rows are re-keyed here (and duplicate_of links with them).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must match app.db.models.SLIDE_ID_NAMESPACE.
SLIDE_ID_NAMESPACE = uuid.UUID("6f1c2a52-9d6e-4c1b-8f0e-2b7a5d0c9e11")


def _rekey_slides() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT s.id, s.page_no, f.subject_id, f.sha256 FROM slides s "
            "JOIN files f ON f.id = s.file_id"
        )
    ).all()
    new_ids = {
        row.id: uuid.uuid5(SLIDE_ID_NAMESPACE, f"{row.subject_id}:{row.sha256}:{row.page_no}")
        for row in rows
    }
    op.drop_constraint("fk_slides_duplicate_of_slides", "slides", type_="foreignkey")
    for old, new in new_ids.items():
        bind.execute(
            sa.text("UPDATE slides SET id = :new WHERE id = :old"), {"new": new, "old": old}
        )
        bind.execute(
            sa.text("UPDATE slides SET duplicate_of = :new WHERE duplicate_of = :old"),
            {"new": new, "old": old},
        )
    op.create_foreign_key(
        "fk_slides_duplicate_of_slides",
        "slides",
        "slides",
        ["duplicate_of"],
        ["id"],
        ondelete="SET NULL",
    )


def upgrade() -> None:
    _rekey_slides()

    op.create_table(
        "transcript_decks",
        sa.Column(
            "transcript_file_id",
            sa.Uuid(),
            sa.ForeignKey(
                "files.id", ondelete="CASCADE", name="fk_transcript_decks_transcript_file_id_files"
            ),
            primary_key=True,
        ),
        sa.Column(
            "slides_file_id",
            sa.Uuid(),
            sa.ForeignKey(
                "files.id", ondelete="CASCADE", name="fk_transcript_decks_slides_file_id_files"
            ),
            primary_key=True,
        ),
        sa.Column("position", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(8), nullable=False, server_default="auto"),
        sa.CheckConstraint("source IN ('auto', 'manual')", name="ck_transcript_decks_source_valid"),
    )

    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey(
                "files.id", ondelete="CASCADE", name="fk_transcript_segments_file_id_files"
            ),
            nullable=False,
        ),
        sa.Column("idx", sa.SmallInteger(), nullable=False),
        sa.Column("start_ms", sa.Integer()),
        sa.Column("end_ms", sa.Integer()),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("clean_text", sa.Text(), nullable=False),
        sa.Column("clean_status", sa.String(16), nullable=False),
        sa.Column("clean_note", sa.Text()),
        sa.Column("low_conf_words", JSONB(), nullable=False, server_default="[]"),
        sa.Column("flags", JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "slide_id",
            sa.Uuid(),
            sa.ForeignKey(
                "slides.id", ondelete="SET NULL", name="fk_transcript_segments_slide_id_slides"
            ),
        ),
        sa.Column("align_score", sa.Float()),
        sa.Column("align_confidence", sa.Float()),
        sa.Column("model", sa.String(64)),
        sa.Column("prompt_version", sa.String(32)),
        sa.Column("request_hash", sa.String(64)),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float()),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("file_id", "idx", name="uq_transcript_segments_file_idx"),
        sa.CheckConstraint(
            "clean_status IN ('cleaned', 'rejected', 'failed')",
            name="ck_transcript_segments_clean_status_valid",
        ),
    )
    op.create_index("ix_transcript_segments_slide_id", "transcript_segments", ["slide_id"])


def downgrade() -> None:
    op.drop_index("ix_transcript_segments_slide_id", table_name="transcript_segments")
    op.drop_table("transcript_segments")
    op.drop_table("transcript_decks")
    # Derived slide ids are kept; they are valid UUIDs either way.
