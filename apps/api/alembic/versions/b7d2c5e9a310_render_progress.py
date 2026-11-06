"""Add render.progress so a long export can report where it is.

Rendering a 105-minute episode takes about twelve minutes of wall clock. Until
this column existed the only observable states were "running" and "done", which
on screen is indistinguishable from a hang — the user reported it as
「点击后无反应」.

Progress is a float 0..1 written by the encoder's own ``-progress`` stream.
Existing rows keep 0.0; every finished render is by definition at 1.0, so the
backfill below sets exactly that and the column stays meaningful for old rows.

The upgrade is **idempotent**: ``tests/test_migrations.py`` rewinds
``alembic_version`` and replays, and without the guard that replay dies on
``duplicate column name``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b7d2c5e9a310"
down_revision = "4e18c2a9f7b1"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(str(row[1]) == column for row in rows)


def upgrade() -> None:
    if not _has_column("render", "progress"):
        with op.batch_alter_table("render") as batch:
            batch.add_column(
                sa.Column("progress", sa.Float(), nullable=False, server_default="0")
            )
    if not _has_column("render", "error"):
        with op.batch_alter_table("render") as batch:
            batch.add_column(sa.Column("error", sa.Text(), nullable=True))
    # A row that says "done" was finished; saying it is 0 % done is a lie the UI
    # would repeat for every historical export.
    op.get_bind().execute(sa.text("UPDATE render SET progress = 1.0 WHERE status = 'done'"))


def downgrade() -> None:
    if _has_column("render", "error"):
        with op.batch_alter_table("render") as batch:
            batch.drop_column("error")
    if _has_column("render", "progress"):
        with op.batch_alter_table("render") as batch:
            batch.drop_column("progress")
