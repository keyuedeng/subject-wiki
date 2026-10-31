"""tutorial_sections: tutorial and solutions sheets split into labelled sections

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tutorial_sections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey(
                "files.id", ondelete="CASCADE", name="fk_tutorial_sections_file_id_files"
            ),
            nullable=False,
        ),
        sa.Column("idx", sa.SmallInteger(), nullable=False),
        sa.Column("number", sa.SmallInteger()),
        sa.Column("is_exercise", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("text_md", sa.Text(), nullable=False),
        sa.Column("words", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_start", sa.SmallInteger()),
        sa.Column("page_end", sa.SmallInteger()),
        sa.Column("section_type", sa.String(16)),
        sa.Column("coverage", sa.String(16)),
        sa.Column("concepts", JSONB(), nullable=False, server_default="[]"),
        sa.Column("earliest_week", sa.SmallInteger()),
        sa.Column(
            "solution_section_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tutorial_sections.id",
                ondelete="SET NULL",
                name="fk_tutorial_sections_solution_section_id_tutorial_sections",
            ),
        ),
        sa.Column("solution_source", sa.String(16), nullable=False, server_default="none"),
        sa.Column("prompt_version", sa.String(32)),
        sa.Column("cost_usd", sa.Float()),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("file_id", "idx", name="uq_tutorial_sections_file_idx"),
        sa.CheckConstraint(
            "section_type IN ('explanation', 'worked_example', 'exercise', 'setup_admin')",
            name="ck_tutorial_sections_section_type_valid",
        ),
        sa.CheckConstraint(
            "coverage IN ('application', 'new_concept')", name="ck_tutorial_sections_coverage_valid"
        ),
        sa.CheckConstraint(
            "solution_source IN ('official', 'generated', 'none')",
            name="ck_tutorial_sections_solution_source_valid",
        ),
    )
    op.create_index("ix_tutorial_sections_file_id", "tutorial_sections", ["file_id"])


def downgrade() -> None:
    op.drop_index("ix_tutorial_sections_file_id", table_name="tutorial_sections")
    op.drop_table("tutorial_sections")
