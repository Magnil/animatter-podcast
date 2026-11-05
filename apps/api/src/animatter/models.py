"""SQLAlchemy schema mirroring AGENT.md §5.1.

Conventions enforced here:

* Primary keys are TEXT ids produced by :func:`animatter.ids.new_id`.
* Timestamps are TEXT ISO-8601 UTC — SQLite has no native timestamp type and
  strings survive JSON round-trips without timezone ambiguity.
* Durations and offsets are INTEGER milliseconds. Never floats (AGENT.md §16.2).

The ``token`` table is append-only by design; ``sentence`` holds the only
mutable structure (ranges over tokens).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base shared by the app and Alembic."""


class Project(Base):
    __tablename__ = "project"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)


class Media(Base):
    __tablename__ = "media"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    sample_rate: Mapped[int] = mapped_column(Integer, default=0)
    channels: Mapped[int] = mapped_column(Integer, default=0)
    codec: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(32), default="original")
    created_at: Mapped[str] = mapped_column(String(32))


class Speaker(Base):
    """Global speaker identity for a project; diarization emits local ids that map here."""

    __tablename__ = "speaker"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    color: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[str] = mapped_column(String(32))


class TranscribeJob(Base):
    __tablename__ = "transcribe_job"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"), index=True)
    media_id: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(120))
    #: exact request parameters sent to the provider (hotwords, context, headers)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    oss_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_json: Mapped[str] = mapped_column(Text, default="{}")
    raw_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # --- chunked transcription (>2h sources, AGENT.md §6.2) -----------
    parent_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    segment_index: Mapped[int] = mapped_column(Integer, default=0)
    segment_start_ms: Mapped[int] = mapped_column(Integer, default=0)
    segment_end_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(32))
    finished_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class TranscriptVersion(Base):
    __tablename__ = "transcript_version"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"), index=True)
    media_id: Mapped[str] = mapped_column(String(64))
    job_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[str] = mapped_column(String(32))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    sentence_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)


class Token(Base):
    """Immutable token stream produced by one transcript version."""

    __tablename__ = "token"
    __table_args__ = (Index("ix_token_version_idx", "version_id", "idx", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("transcript_version.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16), default="char")
    src_word_idx: Mapped[int] = mapped_column(Integer, default=-1)
    punc_after: Mapped[str] = mapped_column(Text, default="")
    derived: Mapped[bool] = mapped_column(Boolean, default=True)


class Sentence(Base):
    __tablename__ = "sentence"
    __table_args__ = (Index("ix_sentence_version_ord", "version_id", "ord", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("transcript_version.id", ondelete="CASCADE"), index=True
    )
    ord: Mapped[int] = mapped_column(Integer)
    token_start: Mapped[int] = mapped_column(Integer)
    token_end: Mapped[int] = mapped_column(Integer)
    speaker_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    origin: Mapped[str] = mapped_column(String(32), default="asr")
    updated_at: Mapped[str] = mapped_column(String(32))


class EditOp(Base):
    __tablename__ = "edit_op"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    version_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    op_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(32))
    undone: Mapped[bool] = mapped_column(Boolean, default=False)


class Deletion(Base):
    __tablename__ = "deletion"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(64), index=True)
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16), default="token")
    token_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[str] = mapped_column(String(32))
    removed: Mapped[bool] = mapped_column(Boolean, default=False)


class Render(Base):
    __tablename__ = "render"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    version_id: Mapped[str] = mapped_column(String(64), index=True)
    edl_hash: Mapped[str] = mapped_column(String(64))
    out_path: Mapped[str] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    created_at: Mapped[str] = mapped_column(String(32))


class Export(Base):
    __tablename__ = "export"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(Text)
    options_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[str] = mapped_column(String(32))


class Hotword(Base):
    """Project glossary; ``project_id IS NULL`` means the shared global glossary."""

    __tablename__ = "hotword"
    __table_args__ = (UniqueConstraint("project_id", "text", name="uq_hotword_scope_text"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    text: Mapped[str] = mapped_column(String(64))
    weight: Mapped[int] = mapped_column(Integer, default=4)
    lang: Mapped[str] = mapped_column(String(8), default="zh")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tags_json: Mapped[str] = mapped_column(Text, default="[]")
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    miss_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))


class HotwordSync(Base):
    """Remote mapping for the pre-compiled vocabulary (50-list / 5-minute constraints)."""

    __tablename__ = "hotword_sync"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    vocabulary_id: Mapped[str] = mapped_column(String(128), default="")
    target_model: Mapped[str] = mapped_column(String(120), default="")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    dirty: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    next_allowed_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class EpisodeHotword(Base):
    """Per-episode instant hotwords (``parameters.vocabulary``)."""

    __tablename__ = "episode_hotword"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    media_id: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(String(64))
    weight: Mapped[int] = mapped_column(Integer, default=4)
    lang: Mapped[str] = mapped_column(String(8), default="zh")
    origin: Mapped[str] = mapped_column(String(16), default="manual")
    evidence: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Outline(Base):
    __tablename__ = "outline"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    media_id: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(32))


class Job(Base):
    __tablename__ = "job"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    progress: Mapped[float] = mapped_column(default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class CorrectionCandidate(Base):
    """Model-proposed text repair awaiting human review (AGENT.md §6.5).

    Nothing is rewritten automatically: a candidate records what the model thinks
    is wrong, what it proposes, and which hotword justifies the change. Only
    candidates tied to a known term are surfaced, which is what keeps
    hallucinated edits out of the review queue.
    """

    __tablename__ = "correction_candidate"
    __table_args__ = (Index("ix_correction_version_status", "version_id", "status"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("transcript_version.id", ondelete="CASCADE"), index=True
    )
    sentence_ord: Mapped[int] = mapped_column(Integer, index=True)

    #: Token range this candidate may rewrite: [token_start, token_end)
    token_start: Mapped[int] = mapped_column(Integer)
    token_end: Mapped[int] = mapped_column(Integer)

    original_text: Mapped[str] = mapped_column(Text)
    proposed_text: Mapped[str] = mapped_column(Text)
    matched_hotword: Mapped[str] = mapped_column(String(64), default="")

    #: 0..1 from the model; used for ordering only, never for auto-accept.
    confidence: Mapped[float] = mapped_column(default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")

    #: pending | accepted | rejected | stale
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    source: Mapped[str] = mapped_column(String(16), default="llm")

    created_at: Mapped[str] = mapped_column(String(32))
    resolved_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
