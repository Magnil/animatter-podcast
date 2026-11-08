"""Add transcript_version.time_shift_ms for the 时间轴对齐 offset.

Why the column exists at all, given that the token and deletion rows already
hold shifted times: without it the offset is *unobservable*. The UI has to say
how far the current axis has been moved (so the user can go back to 0 or try a
different value), and "subtract 80 from every row again" is not a value — it is
a guess about how many times it has been applied.

Existing rows get 0, which is exactly right: every version transcribed before
this column existed still holds the ASR's own timestamps.

The upgrade is **idempotent** — ``tests/test_migrations.py`` rewinds
``alembic_version`` and replays, and without the guard that replay dies on
``duplicate column name``.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c1f70e2b9d84"
down_revision = "b7d2c5e9a310"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(str(row[1]) == column for row in rows)


def upgrade() -> None:
    if _has_column("transcript_version", "time_shift_ms"):
        return
    with op.batch_alter_table("transcript_version") as batch:
        batch.add_column(
            sa.Column("time_shift_ms", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    if not _has_column("transcript_version", "time_shift_ms"):
        return
    with op.batch_alter_table("transcript_version") as batch:
        batch.drop_column("time_shift_ms")
