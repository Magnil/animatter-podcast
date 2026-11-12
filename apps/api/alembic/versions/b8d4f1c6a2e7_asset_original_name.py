"""``video_asset.original_name`` — the name the file had before we renamed it by hash (§19.12).

Round 104: the asset library showed ``b825e065e6cf9826.png`` because the copy inside the
project is named by content hash, and two imported stills were indistinguishable — both in
the library and on the video timeline. The hash has to stay the *file* name (two
``IMG_0001.png`` from different folders must not collide), so the display name is kept
next to it instead of derived from it.

Default **empty string**, meaning "this row predates the column": the display falls back to
the hash name, which is exactly what those rows showed before, so nothing changes for
existing projects until a file is imported again.

Revision ID: b8d4f1c6a2e7
Revises: a7c3e9f14b26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4f1c6a2e7"
down_revision: str | None = "a7c3e9f14b26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("video_asset", "original_name"):
        op.add_column(
            "video_asset",
            sa.Column("original_name", sa.String(200), nullable=False, server_default=""),
        )


def downgrade() -> None:
    if _has_column("video_asset", "original_name"):
        op.drop_column("video_asset", "original_name")
