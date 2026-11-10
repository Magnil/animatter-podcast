"""Add ``token.hide_source`` so batch-hid fillers can be restored separately

Batch cleanup can now *hide* fillers instead of deleting them, and the user needs
"还原已隐去口癖词" to bring those back **without** touching words they hid one at
a time with Backspace. A bare ``hidden`` boolean cannot tell those apart.

Two rejected alternatives:

* Recording the token indices in the ``edit_op`` payload and un-hiding those.
  Wrong once anything renumbers tokens: a later text merge shifts ``idx`` and the
  recorded list points at the wrong words. The state belongs on the row.
* Treating every hidden word as restorable. That silently throws away manual
  decisions — the user hid «呃» on purpose while reading that sentence.

Revision ID: b3d7e9a1c4f2
Revises: 8f2a4c1d9e70
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3d7e9a1c4f2"
down_revision: str | None = "8f2a4c1d9e70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"PRAGMA table_info({table})")).fetchall()
    return any(str(row[1]) == column for row in rows)


def upgrade() -> None:
    if _has_column("token", "hide_source"):
        return
    # ``''`` means "a human hid this word by hand"; ``'filler'`` means the batch
    # cleanup did it. Defaulting to ``''`` keeps every existing hidden word in
    # the manual bucket, which is the safe side: nothing gets bulk-restored that
    # the user did not ask for.
    with op.batch_alter_table("token") as batch:
        batch.add_column(
            sa.Column("hide_source", sa.String(length=16), nullable=False, server_default=sa.text("''"))
        )


def downgrade() -> None:
    if not _has_column("token", "hide_source"):
        return
    with op.batch_alter_table("token") as batch:
        batch.drop_column("hide_source")
