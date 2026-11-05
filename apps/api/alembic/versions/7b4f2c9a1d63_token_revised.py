"""Add ``token.revised`` so corrections are visible at a glance

The editor colours a word blue when a human rewrote it and red when the word was
deleted. Deletions are already representable (``deletion`` rows address token
ranges), but "this word used to say something else" had no home: it was only
in the append-only ``edit_op`` log, and that log is index-based while text edits
renumber ``idx``.

Storing the flag on the token keeps the rendering decision local and survives a
re-import through the archive.

Revision ID: 7b4f2c9a1d63
Revises: 3c1a7d90e5b2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7b4f2c9a1d63"
down_revision: str | None = "3c1a7d90e5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``server_default`` is what makes the column NOT NULL without a table
    # rebuild: SQLite cannot add a NOT NULL column without a default, and every
    # existing token was produced by the ASR, so 0 is the correct value.
    with op.batch_alter_table("token") as batch:
        batch.add_column(
            sa.Column("revised", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )


def downgrade() -> None:
    with op.batch_alter_table("token") as batch:
        batch.drop_column("revised")
