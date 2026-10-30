"""pairing_suggestions: text-similarity deck suggestions for transcripts to confirm

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pairing_suggestions",
        sa.Column(
            "transcript_file_id",
            sa.Uuid(),
            sa.ForeignKey(
                "files.id",
                ondelete="CASCADE",
                name="fk_pairing_suggestions_transcript_file_id_files",
            ),
            primary_key=True,
        ),
        sa.Column(
            "slides_file_id",
            sa.Uuid(),
            sa.ForeignKey(
                "files.id", ondelete="CASCADE", name="fk_pairing_suggestions_slides_file_id_files"
            ),
            nullable=False,
        ),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("runner_up", sa.Float()),
        sa.Column("candidates", JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("pairing_suggestions")
