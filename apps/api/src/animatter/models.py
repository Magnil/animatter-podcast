"""SQLAlchemy schema mirroring AGENTS.md §5.1.

Conventions enforced here:

* Primary keys are TEXT ids produced by :func:`animatter.ids.new_id`.
* Timestamps are TEXT ISO-8601 UTC — SQLite has no native timestamp type and
  strings survive JSON round-trips without timezone ambiguity.
* Durations and offsets are INTEGER milliseconds. Never floats (AGENTS.md §16.2).

The ``token`` table is append-only by design; ``sentence`` holds the only
mutable structure (ranges over tokens).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Float,
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
    """One audio file, owned by one project.

    ``sha256`` is unique **per project**, not globally. Media rows are
    project-owned, so a global unique constraint is self-contradictory: the same
    audio used by two projects forces them to share a row, which breaks
    ownership — the second project ends up with no media of its own and the
    audio route (which filters by project) returns nothing.
    """

    __tablename__ = "media"
    __table_args__ = (UniqueConstraint("project_id", "sha256", name="uq_media_project_sha256"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    sample_rate: Mapped[int] = mapped_column(Integer, default=0)
    channels: Mapped[int] = mapped_column(Integer, default=0)
    codec: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(32), default="original")
    created_at: Mapped[str] = mapped_column(String(32))


class Speaker(Base):
    """A named speaker within one project.

    ``local_id`` is the integer that ``sentence.speaker_id`` stores. Diarization
    emits those integers and the editor lets the user name them, so the integer
    must stay stable per project — renaming a speaker must not rewrite every
    sentence, and merging two detected speakers must stay possible.
    """

    __tablename__ = "speaker"
    __table_args__ = (UniqueConstraint("project_id", "local_id", name="uq_speaker_project_local"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"), index=True)
    #: Integer carried by ``sentence.speaker_id``; 0-based, dense per project.
    local_id: Mapped[int] = mapped_column(Integer, default=0)
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
    # --- chunked transcription (>2h sources, AGENTS.md §6.2) -----------
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
    #: How far this version's whole timeline has been shifted from what the ASR
    #: reported, in ms (negative = moved earlier). Only a *record* of what was
    #: applied: the token and deletion rows already hold the shifted times, and a
    #: new value is applied as ``target - current``. Storing the delta rather than
    #: recomputing from the raw response is what lets a human's own edge drags
    #: survive a realignment instead of being silently reset.
    time_shift_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


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
    #: True once a human has rewritten this token's text. Stored on the row
    #: rather than derived from ``edit_op``: history is index-sensitive (later
    #: edits renumber ``idx``), and the editor colours *what was written* versus
    #: *what was observed*, which stays true even after an undo.
    revised: Mapped[bool] = mapped_column(Boolean, default=False)
    #: True once a human hid this word **from the text output only** — it stays
    #: in the audio. This is the answer to filler words («嗯», «那个»): cutting
    #: their audio makes the speech sound chopped, but leaving them in the
    #: subtitles looks sloppy. A third state is needed because neither existing
    #: one is right: ``deletion`` removes sound, ``revised`` rewrites text.
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Who hid it: ``''`` for a hand-picked word (Backspace in the editor),
    #: ``'filler'`` for the batch cleanup. Restore-by-category only ever touches
    #: the batch ones — a bulk "take back the hiding" must not also undo the words
    #: the user hid deliberately while reading.
    hide_source: Mapped[str] = mapped_column(String(16), default="")


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
    #: One-click cleanup writes many rows in one transaction; the batch id is
    #: what makes "undo that cleanup" a single operation instead of 400.
    batch_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)


class Render(Base):
    __tablename__ = "render"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), index=True)
    version_id: Mapped[str] = mapped_column(String(64), index=True)
    edl_hash: Mapped[str] = mapped_column(String(64))
    out_path: Mapped[str] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    #: 0..1 of the encoded *output* timeline. A 105-minute export runs for about
    #: twelve minutes; without this the UI can only say "still going", which is
    #: what made a working export look like a dead button.
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    #: Failure text for ``status == 'failed'``; NULL in every other state.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    """Model-proposed text repair awaiting human review (AGENTS.md §6.5).

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


class Setting(Base):
    """Single-row JSON blob for user-adjustable settings.

    One row with a JSON payload rather than a column per option: the set of
    tunables keeps growing, and a blob means adding one needs no migration.
    """

    __tablename__ = "setting"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[str] = mapped_column(String(32))


# --- video (AGENTS.md §19) ---------------------------------------------


class VideoTemplate(Base):
    """A compiled PSD template: background, overlay, and slot geometry.

    The compile step is **once per import**, not once per render. Measured cost is
    5–20 s per ``CompositeEngine`` pass on a 2560×1440 58-layer file, which is
    fine as a one-off and impossible inside an export.

    ``source_sha256`` is what makes re-importing the same file free: the compiled
    PNGs depend only on the PSD bytes, so a matching hash can reuse them.
    """

    __tablename__ = "video_template"
    __table_args__ = (Index("ix_video_template_project", "project_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True
    )
    source_path: Mapped[str] = mapped_column(Text, default="")
    source_sha256: Mapped[str] = mapped_column(String(64), default="")
    canvas_w: Mapped[int] = mapped_column(Integer, default=0)
    canvas_h: Mapped[int] = mapped_column(Integer, default=0)
    #: The poster with the media-box plate but no content, avatars and all text
    #: flattened out. Rendered by us, never re-read from the PSD.
    background_path: Mapped[str] = mapped_column(Text, default="")
    #: The layers that must sit *above* the box content (its border/stroke).
    overlay_path: Mapped[str] = mapped_column(Text, default="")
    #: ``{"x","y","w","h"}`` in canvas coordinates — where content goes.
    box_rect: Mapped[str] = mapped_column(Text, default="{}")
    box_fit_default: Mapped[str] = mapped_column(String(16), default="blur_fill")
    #: ``{"avatars": [...], "texts": [...]}``; see services/video_templates.py.
    slots_json: Mapped[str] = mapped_column(Text, default="{}")
    #: ``{"<slot name>": <speaker local_id>}`` — which speaker each avatar slot shows.
    #: Kept on the row rather than inside ``slots_json`` so a recompile (which rewrites
    #: the slots from the PSD) does not silently drop the user's bindings.
    bindings_json: Mapped[str] = mapped_column(Text, default="{}")
    #: ``{"texts": {<slot key>: {"text","font","colour","size"}},
    #:   "photos": {<slot name>: <asset id>}}`` — the user's edits to the template's own
    #: artwork. A third column for the same reason as ``bindings_json``: ``slots_json`` is
    #: rewritten from the PSD on every compile, so an override stored there would vanish the
    #: first time the same template was re-imported, and the symptom would be "my title went
    #: back to the designer's placeholder" with nothing to explain it.
    edits_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    #: Human-readable reason for ``status == 'failed'``. A compile can fail on a
    #: file psd-tools cannot parse, and "failed" alone sends the user nowhere.
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String(32))
    compiled_at: Mapped[str | None] = mapped_column(String(32), nullable=True)


class VideoAsset(Base):
    """One image or video the user can drop into the media box."""

    __tablename__ = "video_asset"
    __table_args__ = (UniqueConstraint("project_id", "sha256", name="uq_video_asset_project_sha256"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True
    )
    #: ``image`` | ``video``
    kind: Mapped[str] = mapped_column(String(16), default="image")
    path: Mapped[str] = mapped_column(Text)
    #: The name the file had when the user imported it (``开场封面.png``).
    #:
    #: Stored because the copy on disk is named by hash — ``b825e065e6cf9826.png`` — which
    #: makes two imported stills indistinguishable in the library and on the timeline. The
    #: hash stays the file name (two ``IMG_0001.png`` from different folders must not
    #: collide, §19.12); this column is what the UI reads. **Empty** means "this row predates
    #: the column", and the display falls back to the hash name rather than showing nothing.
    original_name: Mapped[str] = mapped_column(String(200), default="")
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    #: 0 for images — they have no intrinsic length, so a block's length is
    #: whatever the timeline says rather than something the asset forces.
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String(32))


class Deck(Base):
    """Our own slide model. Never a ``.pptx`` as the render source (AGENTS.md §19.6)."""

    __tablename__ = "deck"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200), default="")
    #: ``model`` when the pages came from an outline call, ``transcript`` when the
    #: deterministic fallback produced them. Stored rather than recomputed because it is a
    #: fact about *this* generation: after a failed call the panel has to be able to say why
    #: the deck looks like a transcript, and the answer is not visible in the rows.
    source: Mapped[str] = mapped_column(String(16), default="transcript")
    #: JSON list of user-facing strings produced by the generation run (a dropped page, a
    #: merged page, a failed block). Empty for a clean run.
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    #: Typeface for the page title and for the body points, set separately (§19.17).
    #: **Empty means "the renderer's default"**, deliberately not a font name: an untouched deck
    #: then keeps following ``slide_render.FONT_FAMILY`` if that default ever changes, and a
    #: migration does not have to write a machine-specific family into every existing row.
    title_font: Mapped[str] = mapped_column(String(120), default="")
    body_font: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[str] = mapped_column(String(32))


class Slide(Base):
    __tablename__ = "slide"
    __table_args__ = (Index("ix_slide_deck_ord", "deck_id", "ord", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    deck_id: Mapped[str] = mapped_column(ForeignKey("deck.id", ondelete="CASCADE"), index=True)
    ord: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text, default="")
    #: ``title`` | ``title_body`` | ``quote``
    layout: Mapped[str] = mapped_column(String(16), default="title_body")
    #: Which sentence produced this page, so the editor can jump to its source.
    source_ord: Mapped[int] = mapped_column(Integer, default=-1)
    start_ms: Mapped[int] = mapped_column(Integer, default=0)


class VideoItem(Base):
    """One block on the video timeline.

    ``start_ms``/``end_ms`` are **original-timeline milliseconds**, snapped to a
    sentence start when the user drags (AGENTS.md §19.3). They are absolute
    values, not references: measured against real data, every candidate identity
    for "which sentence boundary is this" turned out to be unstable — ``sentence.id``
    follows the *position* because rows are matched by ``ord``, ``token.idx``
    shifts on text edits (31 of 379 real edits changed the token count), and
    ``src_word_idx`` is a word ordinal (83 distinct values across 8180 tokens), not
    an address. An absolute instant only moves on ``align.apply_shift``, which is a
    rigid whole-timeline move and can shift these rows with it.

    The block plays **continuously** in the output: deleting audio inside it
    shortens it and its content loops or truncates. It does not jump-cut.
    """

    __tablename__ = "video_item"
    __table_args__ = (Index("ix_video_item_project_start", "project_id", "start_ms"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("project.id", ondelete="CASCADE"), index=True
    )
    #: ``media`` | ``gallery`` | ``slide``
    kind: Mapped[str] = mapped_column(String(16), default="media")
    start_ms: Mapped[int] = mapped_column(Integer)
    end_ms: Mapped[int] = mapped_column(Integer)
    z: Mapped[int] = mapped_column(Integer, default=0)
    #: ``contain`` | ``cover`` | ``blur_fill`` | ``stretch``
    fit: Mapped[str] = mapped_column(String(16), default="blur_fill")
    #: ``none`` | ``kenburns``
    motion: Mapped[str] = mapped_column(String(16), default="none")
    asset_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deck_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    slide_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Gallery members: ``[{"asset_id": ..., "weight": ...}]``.
    gallery_json: Mapped[str] = mapped_column(Text, default="[]")
    #: The block's audio was deleted in full, so it has zero length in the output.
    #: Flagged rather than deleted: the user may restore that sentence tomorrow.
    orphaned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[str] = mapped_column(String(32))
