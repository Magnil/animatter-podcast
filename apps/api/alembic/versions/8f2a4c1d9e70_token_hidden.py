"""Add ``token.hidden`` so a word can leave the subtitles but stay audible

Three states were being conflated into two. A filler word («嗯», «那个») should
disappear from the published subtitles, but cutting its audio out of the render
makes the speech sound chopped and is not what the user asked for. Neither
existing mechanism fits:

* ``deletion`` removes the audio **and** the text;
* ``revised`` rewrites the text but keeps it;
* ``hidden`` removes the text and leaves the audio alone.

Revision ID: 8f2a4c1d9e70
Revises: c1f70e2b9d84
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8f2a4c1d9e70"
down_revision: str | None = "c1f70e2b9d84"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(str(row[1]) == column for row in rows)


def upgrade() -> None:
    if _has_column("token", "hidden"):
        return
    # Same shape as ``token.revised``: SQLite cannot add a NOT NULL column
    # without a default, and every existing token is visible.
    with op.batch_alter_table("token") as batch:
        batch.add_column(
            sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )


def downgrade() -> None:
    if not _has_column("token", "hidden"):
        return
    with op.batch_alter_table("token") as batch:
        batch.drop_column("hidden")
