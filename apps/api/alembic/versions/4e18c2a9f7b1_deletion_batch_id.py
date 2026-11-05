"""Add deletion.batch_id for one-click cleanup batches.

Revision ID: 4e18c2a9f7b1
Revises: 9d21c4a7f0be
Create Date: 2026-11-06

Why a column and not "just undo the last N rows": the batch is the unit the user
sees. 400 deletions from one click are one line in the history panel and one
undo. Reconstructing that group after the fact (same timestamp, same source) is
guesswork; storing it is one nullable column.

Existing rows get NULL, which reads as "not part of a batch" — correct, since
they were all made one at a time.

The upgrade is **idempotent**. ``tests/test_migrations.py`` deliberately rewinds
``alembic_version`` and replays, which is exactly what a half-failed deploy does;
without the guard that replay dies on ``duplicate column name``.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "4e18c2a9f7b1"
down_revision = "9d21c4a7f0be"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(str(row[1]) == column for row in rows)


def upgrade() -> None:
    if _has_column("deletion", "batch_id"):
        return
    with op.batch_alter_table("deletion") as batch:
        batch.add_column(sa.Column("batch_id", sa.String(length=64), nullable=True))
        batch.create_index("ix_deletion_batch_id", ["batch_id"])


def downgrade() -> None:
    if not _has_column("deletion", "batch_id"):
        return
    with op.batch_alter_table("deletion") as batch:
        batch.drop_index("ix_deletion_batch_id")
        batch.drop_column("batch_id")
