"""llm_cache keyed by request hash (replaces parse_cache); slides.request_hash

parse_cache rows can't be re-keyed (their requests' full text isn't stored), so they are
dropped. Parsed slides keep their content; only a future re-parse is affected.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_cache",
        sa.Column("request_hash", sa.String(64), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32)),
        sa.Column("response_json", JSONB(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.add_column("slides", sa.Column("request_hash", sa.String(64)))
    op.drop_table("parse_cache")


def downgrade() -> None:
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
    op.drop_column("slides", "request_hash")
    op.drop_table("llm_cache")
