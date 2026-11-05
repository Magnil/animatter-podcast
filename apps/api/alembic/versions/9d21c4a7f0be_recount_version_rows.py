"""Recompute the denormalised token/sentence counters on every version

``transcript_version.token_count`` and ``sentence_count`` are caches of the token
and sentence rows. ``token_count`` was maintained, but ``sentence_count`` was
written once at transcription time and never updated by a boundary edit — so an
edited 5-sentence transcript still claimed "3 句" in the header, and the archive
saved the wrong number.

The write paths are fixed (``services/editing.py``). This migration repairs rows
that already drifted, and is written to be safe to run on a correct database
(it recomputes, it does not patch a delta).

Revision ID: 9d21c4a7f0be
Revises: 7b4f2c9a1d63
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d21c4a7f0be"
down_revision: str | None = "7b4f2c9a1d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE transcript_version SET "
            "token_count = (SELECT COUNT(*) FROM token WHERE token.version_id = transcript_version.id), "
            "sentence_count = "
            "(SELECT COUNT(*) FROM sentence WHERE sentence.version_id = transcript_version.id)"
        )
    )


def downgrade() -> None:
    # The previous values were wrong; there is nothing to restore.
    pass
