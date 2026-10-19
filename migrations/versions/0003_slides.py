"""slides, parse_cache; files.status gains 'parsing'; files.page_count

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_files_status_valid", "files", type_="check")
    op.create_check_constraint(
        "ck_files_status_valid", "files", "status IN ('uploaded', 'parsing', 'parsed', 'failed')"
    )
    op.add_column("files", sa.Column("page_count", sa.SmallInteger()))

    op.create_table(
        "slides",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey("files.id", ondelete="CASCADE", name="fk_slides_file_id_files"),
            nullable=False,
        ),
        sa.Column("page_no", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.Text()),
        sa.Column("text_raw", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text()),
        sa.Column("content_json", JSONB(), nullable=False),
        sa.Column("route", sa.String(16), nullable=False),
        sa.Column("route_reason", sa.Text(), nullable=False),
        sa.Column("route_signals", JSONB(), nullable=False),
        sa.Column("image_path", sa.Text(), nullable=False),
        sa.Column(
            "duplicate_of",
            sa.Uuid(),
            sa.ForeignKey("slides.id", ondelete="SET NULL", name="fk_slides_duplicate_of_slides"),
        ),
        sa.Column("model", sa.String(64)),
        sa.Column("prompt_version", sa.String(32)),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float()),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("file_id", "page_no", name="uq_slides_file_page"),
        sa.CheckConstraint(
            "route IN ('text', 'vision', 'duplicate')", name="ck_slides_route_valid"
        ),
    )

    op.create_table(
        "parse_cache",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("page_no", sa.SmallInteger(), primary_key=True),
        sa.Column("prompt_version", sa.String(32), primary_key=True),
        sa.Column("model", sa.String(64), primary_key=True),
        sa.Column("content_json", JSONB(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("parse_cache")
    op.drop_table("slides")
    op.drop_column("files", "page_count")
    op.drop_constraint("ck_files_status_valid", "files", type_="check")
    op.create_check_constraint(
        "ck_files_status_valid", "files", "status IN ('uploaded', 'parsed', 'failed')"
    )
