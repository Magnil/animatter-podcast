"""``video_item.solo`` + ``video_item.placement`` (§19.25, §19.26).

Revision ID: e3a7c1d5b920
Revises: c9e2a71d4f83
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e3a7c1d5b920"
down_revision: str | None = "c9e2a71d4f83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    # Both defaults are the "behave exactly as before" value: no block solos (so the podcast
    # stays audible everywhere), every block placed in the box (so no chain changes).
    if not _has_column("video_item", "solo"):
        op.add_column(
            "video_item",
            sa.Column("solo", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    if not _has_column("video_item", "placement"):
        op.add_column(
            "video_item",
            sa.Column("placement", sa.String(8), nullable=False, server_default="box"),
        )


def downgrade() -> None:
    if _has_column("video_item", "placement"):
        op.drop_column("video_item", "placement")
    if _has_column("video_item", "solo"):
        op.drop_column("video_item", "solo")
