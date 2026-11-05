"""media sha256 unique per project, not globally

Media rows are project-owned: ``register_media`` copies the file into
``data/projects/<project_id>/media/``, so two projects using the same source
audio legitimately own two rows. A global ``UNIQUE (sha256)`` contradicts that.
When it was in force, the second project reused the first project's row, and
every project-scoped query returned nothing for it — which surfaced to the user
as "这个项目没有登记音频文件".

The original constraint was created **inline and unnamed**, and SQLite cannot
drop a constraint at all. So this migration performs the standard SQLite table
rebuild in raw SQL: create the new shape, copy the rows, swap.

Revision ID: 8b3eb15ba250
Revises: 2f380cd1b536
Create Date: 2026-11-06 00:09:32.361928
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "8b3eb15ba250"
down_revision: str | None = "2f380cd1b536"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    "id, project_id, path, sha256, duration_ms, sample_rate, channels, codec, kind, created_at"
)

_CREATE_NEW = """
CREATE TABLE media_new (
    id VARCHAR(64) NOT NULL,
    project_id VARCHAR(64) NOT NULL,
    path TEXT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    duration_ms INTEGER NOT NULL,
    sample_rate INTEGER NOT NULL,
    channels INTEGER NOT NULL,
    codec VARCHAR(64) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE,
    CONSTRAINT uq_media_project_sha256 UNIQUE (project_id, sha256)
)
"""

#: Kept for downgrade: the original shape, with the global unique restored.
_CREATE_OLD = """
CREATE TABLE media_old (
    id VARCHAR(64) NOT NULL,
    project_id VARCHAR(64) NOT NULL,
    path TEXT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    duration_ms INTEGER NOT NULL,
    sample_rate INTEGER NOT NULL,
    channels INTEGER NOT NULL,
    codec VARCHAR(64) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    created_at VARCHAR(32) NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(project_id) REFERENCES project (id) ON DELETE CASCADE,
    UNIQUE (sha256)
)
"""


def upgrade() -> None:
    op.execute(_CREATE_NEW)
    op.execute(f"INSERT INTO media_new ({_COLUMNS}) SELECT {_COLUMNS} FROM media")
    op.execute("DROP TABLE media")
    op.execute("ALTER TABLE media_new RENAME TO media")
    op.execute("CREATE INDEX ix_media_sha256 ON media (sha256)")
    op.execute("CREATE INDEX ix_media_project_id ON media (project_id)")


def downgrade() -> None:
    # A global unique cannot be restored once two projects share a hash; dedupe
    # first so the downgrade does not fail on real data.
    op.execute(_CREATE_OLD)
    op.execute(
        f"""
        INSERT INTO media_old ({_COLUMNS})
        SELECT {_COLUMNS} FROM media m
        WHERE m.id = (SELECT MIN(x.id) FROM media x WHERE x.sha256 = m.sha256)
        """
    )
    op.execute("DROP TABLE media")
    op.execute("ALTER TABLE media_old RENAME TO media")
