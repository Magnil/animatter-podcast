"""Video timeline tables: template, asset, deck/slide, and video_item

Implements ``AGENTS.md §19.4``. Nothing is backfilled: a project has no video
timeline until someone imports a template, so every table starts empty and no
existing row changes.

Why ``video_item`` stores absolute milliseconds rather than a reference to a
sentence boundary — the whole reason this migration can stay this simple:

* ``sentence.id`` looks like a stable identity but is not. ``_persist_spans``
  matches rows by ``ord``, which is positional, so splitting a sentence hands the
  id at that position to whatever sentence now sits there. An anchor stored as an
  id would silently jump to a different sentence.
* ``token.idx`` shifts whenever a text edit changes the token count (31 of 379
  real edits do, by −1..−4).
* ``token.src_word_idx`` is an ASR word ordinal — 83 distinct values across 8180
  tokens — so it cannot address a position at all.

An absolute instant only moves when the whole timeline is shifted
(``align.apply_shift``, a rigid ±1000 ms move), and that path shifts the video
rows alongside the tokens and deletions.

Revision ID: c4e1a7b3f9d2
Revises: b3d7e9a1c4f2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e1a7b3f9d2"
down_revision: str | None = "b3d7e9a1c4f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(table: str) -> bool:
    bind = op.get_bind()
    return table in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    if not _has_table("video_template"):
        op.create_table(
            "video_template",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("source_path", sa.Text(), nullable=False),
            sa.Column("source_sha256", sa.String(length=64), nullable=False),
            sa.Column("canvas_w", sa.Integer(), nullable=False),
            sa.Column("canvas_h", sa.Integer(), nullable=False),
            sa.Column("background_path", sa.Text(), nullable=False),
            sa.Column("overlay_path", sa.Text(), nullable=False),
            sa.Column("box_rect", sa.Text(), nullable=False),
            sa.Column("box_fit_default", sa.String(length=16), nullable=False),
            sa.Column("slots_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("error", sa.Text(), nullable=False),
            sa.Column("created_at", sa.String(length=32), nullable=False),
            sa.Column("compiled_at", sa.String(length=32), nullable=True),
            sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_video_template_project_id", "video_template", ["project_id"], unique=False
        )

    if not _has_table("video_asset"):
        op.create_table(
            "video_asset",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("path", sa.Text(), nullable=False),
            sa.Column("sha256", sa.String(length=64), nullable=False),
            sa.Column("width", sa.Integer(), nullable=False),
            sa.Column("height", sa.Integer(), nullable=False),
            sa.Column("duration_ms", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.String(length=32), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("project_id", "sha256", name="uq_video_asset_project_sha256"),
        )
        op.create_index("ix_video_asset_project_id", "video_asset", ["project_id"], unique=False)
        op.create_index("ix_video_asset_sha256", "video_asset", ["sha256"], unique=False)

    if not _has_table("deck"):
        op.create_table(
            "deck",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("created_at", sa.String(length=32), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_deck_project_id", "deck", ["project_id"], unique=False)

    if not _has_table("slide"):
        op.create_table(
            "slide",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("deck_id", sa.String(length=64), nullable=False),
            sa.Column("ord", sa.Integer(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("layout", sa.String(length=16), nullable=False),
            sa.Column("source_ord", sa.Integer(), nullable=False),
            sa.Column("start_ms", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["deck_id"], ["deck.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_slide_deck_id", "slide", ["deck_id"], unique=False)
        op.create_index("ix_slide_deck_ord", "slide", ["deck_id", "ord"], unique=True)

    if not _has_table("video_item"):
        op.create_table(
            "video_item",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("start_ms", sa.Integer(), nullable=False),
            sa.Column("end_ms", sa.Integer(), nullable=False),
            sa.Column("z", sa.Integer(), nullable=False),
            sa.Column("fit", sa.String(length=16), nullable=False),
            sa.Column("motion", sa.String(length=16), nullable=False),
            sa.Column("asset_id", sa.String(length=64), nullable=True),
            sa.Column("deck_id", sa.String(length=64), nullable=True),
            sa.Column("slide_id", sa.String(length=64), nullable=True),
            sa.Column("gallery_json", sa.Text(), nullable=False),
            sa.Column("orphaned", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.String(length=32), nullable=False),
            sa.Column("updated_at", sa.String(length=32), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_video_item_project_id", "video_item", ["project_id"], unique=False
        )
        op.create_index(
            "ix_video_item_project_start",
            "video_item",
            ["project_id", "start_ms"],
            unique=False,
        )


def downgrade() -> None:
    for table in ("video_item", "slide", "deck", "video_asset", "video_template"):
        if _has_table(table):
            op.drop_table(table)
