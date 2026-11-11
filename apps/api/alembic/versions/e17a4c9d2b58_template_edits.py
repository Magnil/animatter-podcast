"""``video_template.edits_json`` — the user's text and avatar-photo overrides.

A third column beside ``slots_json`` and ``bindings_json``, for the same reason both of those
exist: ``slots_json`` is rewritten from the PSD on every compile, and the title the user typed
must not be replaced by the designer's placeholder when the template is re-imported.

Nothing is backfilled. An existing template has no overrides, which renders exactly what the
previous version did — the PSD's own text, drawn by us.

Revision ID: e17a4c9d2b58
Revises: d5f2b8c1e6a3
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e17a4c9d2b58"
down_revision: str | None = "d5f2b8c1e6a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("video_template", "edits_json"):
        op.add_column(
            "video_template",
            sa.Column("edits_json", sa.Text(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    if _has_column("video_template", "edits_json"):
        op.drop_column("video_template", "edits_json")
