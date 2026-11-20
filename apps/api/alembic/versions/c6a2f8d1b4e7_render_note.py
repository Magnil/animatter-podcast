"""``render.note`` — say *why* an export is running with one worker (D6).

The plan is derived from the machine's free memory and the film's chunk shape (§19.34), so it
changes between two exports of the same project. Until now the only place that decision existed
was a log line, which the user never sees; the menu entry could say「正在导出… 37 %」for twenty
minutes without saying whether the machine is serialising because it has to.

Empty for every existing row: an export from before this column had no note to record, and a
blank note renders as "no explanation", which is exactly right.

Revision ID: c6a2f8d1b4e7
Revises: e3a7c1d5b920
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6a2f8d1b4e7"
down_revision: str | None = "e3a7c1d5b920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("render", "note"):
        op.add_column(
            "render",
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    if _has_column("render", "note"):
        with op.batch_alter_table("render") as batch:
            batch.drop_column("note")
