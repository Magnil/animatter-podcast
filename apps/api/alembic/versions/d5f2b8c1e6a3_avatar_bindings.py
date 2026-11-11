"""``video_template.bindings_json`` — which speaker each avatar slot shows (P3).

A separate column, not a key inside ``slots_json``, for one concrete reason: ``slots_json``
is rewritten from the PSD on every compile. A binding stored there would vanish the first
time someone re-imports the same template, and the symptom would be 「头像不跟着说话了」
three sessions later with nothing in the UI to explain it.

Nothing is backfilled: an existing template simply has no bindings, which renders exactly
what P1/P2 did (the PSD's own static photos).

Revision ID: d5f2b8c1e6a3
Revises: c4e1a7b3f9d2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f2b8c1e6a3"
down_revision: str | None = "c4e1a7b3f9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("video_template", "bindings_json"):
        # ``server_default`` rather than a backfill loop: SQLite cannot add a NOT NULL
        # column without one, and "{}" is the correct value for every existing row.
        op.add_column(
            "video_template",
            sa.Column("bindings_json", sa.Text(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    if _has_column("video_template", "bindings_json"):
        op.drop_column("video_template", "bindings_json")
