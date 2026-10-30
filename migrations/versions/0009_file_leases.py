"""files.lease_owner, files.lease_until: which process is working on a file

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("files", sa.Column("lease_owner", sa.String(80)))
    op.add_column("files", sa.Column("lease_until", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("files", "lease_until")
    op.drop_column("files", "lease_owner")
