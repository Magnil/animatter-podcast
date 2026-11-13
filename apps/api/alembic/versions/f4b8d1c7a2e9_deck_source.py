"""``deck.source`` / ``deck.warnings_json`` — how the pages were planned (§19.15).

Round 100 replaced the deterministic page cutter with a topic outline produced by the model.
Two consequences need somewhere to live:

* **which path produced the current pages**, because the fallback still exists and its output
  looks identical in the rows (a title and a body) while being a completely different thing;
* **what the planner had to repair**, which is the only signal that the deck is worse than it
  should be. Dropped pages, merged pages and failed blocks are all invisible afterwards.

Nothing is backfilled beyond the default. An existing deck was cut from the transcript, so
``source='transcript'`` is the correct label for every row already there — the plain server
default is the accurate answer, not a placeholder.

Revision ID: f4b8d1c7a2e9
Revises: e17a4c9d2b58
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4b8d1c7a2e9"
down_revision: str | None = "e17a4c9d2b58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("deck", "source"):
        op.add_column(
            "deck",
            sa.Column("source", sa.String(16), nullable=False, server_default="transcript"),
        )
    if not _has_column("deck", "warnings_json"):
        op.add_column(
            "deck",
            sa.Column("warnings_json", sa.Text(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    if _has_column("deck", "warnings_json"):
        op.drop_column("deck", "warnings_json")
    if _has_column("deck", "source"):
        op.drop_column("deck", "source")
