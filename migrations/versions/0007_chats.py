"""chats and chat_messages: persisted Ask conversations

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chats",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "subject_id",
            sa.Uuid(),
            sa.ForeignKey("subjects.id", ondelete="CASCADE", name="fk_chats_subject_id_subjects"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_chats_subject_id", "chats", ["subject_id"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "chat_id",
            sa.Uuid(),
            sa.ForeignKey("chats.id", ondelete="CASCADE", name="fk_chat_messages_chat_id_chats"),
            nullable=False,
        ),
        sa.Column("idx", sa.SmallInteger(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(16)),
        sa.Column("filters", JSONB(), nullable=False, server_default="{}"),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("chat_id", "idx", name="uq_chat_messages_chat_idx"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_messages_role_valid"),
        sa.CheckConstraint(
            "kind IN ('question', 'answer', 'where', 'error')", name="ck_chat_messages_kind_valid"
        ),
    )


def downgrade() -> None:
    op.drop_table("chat_messages")
    op.drop_table("chats")
