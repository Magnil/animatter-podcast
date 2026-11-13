"""``video_item.muted`` + ``video_asset.has_audio`` (§19.19).

Revision ID: c9e2a71d4f83
Revises: b8d4f1c6a2e7
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e2a71d4f83"
down_revision: str | None = "b8d4f1c6a2e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    # Both defaults are the "behave exactly as before" value, so an existing project's film
    # sounds identical after the migration: every block muted, no asset claiming audio.
    if not _has_column("video_item", "muted"):
        op.add_column(
            "video_item",
            sa.Column("muted", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    if not _has_column("video_asset", "has_audio"):
        op.add_column(
            "video_asset",
            sa.Column("has_audio", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    if _has_column("video_asset", "has_audio"):
        op.drop_column("video_asset", "has_audio")
    if _has_column("video_item", "muted"):
        op.drop_column("video_item", "muted")
