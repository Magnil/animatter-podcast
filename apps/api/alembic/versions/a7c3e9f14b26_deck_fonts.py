"""``deck.title_font`` / ``deck.body_font`` — per-deck slide typefaces (§19.17).

Round 101: a slide's title and its body are different kinds of text, and the deck had exactly
one hard-coded family for both. The template tab has been able to set them separately since
§19.14, so the deck was the one place in the product where type was all-or-nothing.

Both columns default to the **empty string**, which means "whatever the renderer's default
is". Two consequences worth stating:

* an existing deck keeps rendering byte-identically, because nothing has been set;
* the default stays a code constant instead of being frozen into 30 database rows, so changing
  ``slide_render.FONT_FAMILY`` later still affects decks nobody has customised.

Revision ID: a7c3e9f14b26
Revises: f4b8d1c7a2e9
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e9f14b26"
down_revision: str | None = "f4b8d1c7a2e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("deck", "title_font"):
        op.add_column(
            "deck",
            sa.Column("title_font", sa.String(120), nullable=False, server_default=""),
        )
    if not _has_column("deck", "body_font"):
        op.add_column(
            "deck",
            sa.Column("body_font", sa.String(120), nullable=False, server_default=""),
        )


def downgrade() -> None:
    if _has_column("deck", "body_font"):
        op.drop_column("deck", "body_font")
    if _has_column("deck", "title_font"):
        op.drop_column("deck", "title_font")
