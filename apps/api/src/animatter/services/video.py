"""Video timeline blocks and the sentence-boundary grid they snap to (§19.3).

Two rules drive everything in this module.

**Blocks store absolute milliseconds, not sentence references.** Measured against
real data, every candidate identity for "which sentence boundary is this" turned
out to be unstable: ``sentence.id`` follows the *position* (``_persist_spans``
matches rows by ``ord``), ``token.idx`` shifts whenever a text edit changes the
token count (31 of 379 real edits), and ``token.src_word_idx`` is a word ordinal
(83 distinct values across 8180 tokens). An absolute instant only moves when the
whole timeline is shifted, which is a separate, explicit operation.

**Blocks play continuously.** Deleting audio inside a block shortens it in the
output; the content loops or truncates. It does not jump-cut, so the block's
length in the output is ``kept_ms`` over the compiled keep list — never the
difference of its own edges (snapping to silence plus the 20 ms guard moves real
cut points off the boundary).

Nothing here rejects an audio edit. A deletion that crosses a boundary merely
invalidates it, and :func:`refresh_anchors` re-seats the affected edges afterwards.
Refusing the cut would let a video clip veto audio editing, which is backwards: the
video follows the audio.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.video_anchor import (
    Interval,
    boundary_grid,
    kept_ms,
    orphaned_blocks,
    renormalize,
    snap,
    snap_targets,
)
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.models import Media, Sentence, Token, TranscriptVersion, VideoItem

log = get_logger(__name__)

ITEM_KINDS = frozenset({"media", "gallery", "slide"})
FITS = frozenset({"contain", "cover", "blur_fill", "stretch"})
MOTIONS = frozenset({"none", "kenburns"})


@dataclass(frozen=True)
class Grid:
    """The snap grid plus what the UI needs to draw it."""

    boundaries: tuple[int, ...]
    targets: tuple[int, ...]
    duration_ms: int


@dataclass(frozen=True)
class ItemView:
    """One block as the timeline needs it: original edges *and* output length."""

    item: VideoItem
    output_ms: int
    orphaned: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.item.id,
            "kind": self.item.kind,
            "start_ms": self.item.start_ms,
            "end_ms": self.item.end_ms,
            "duration_ms": self.item.end_ms - self.item.start_ms,
            "output_ms": self.output_ms,
            "z": self.item.z,
            "fit": self.item.fit,
            "motion": self.item.motion,
            "asset_id": self.item.asset_id,
            "deck_id": self.item.deck_id,
            "slide_id": self.item.slide_id,
            "gallery_json": self.item.gallery_json,
            "orphaned": self.orphaned,
        }


# --- the grid -----------------------------------------------------------


def sentence_starts(session: Session, version_id: str) -> list[int]:
    """Start instant of every sentence, in sentence order.

    Uses the sentence's first token rather than the previous sentence's end: real
    episodes have gaps between sentences (median 320 ms, max 5.4 s) and the block
    boundary the user aims at is the moment speech resumes, not the trailing
    silence.
    """
    rows = session.execute(
        select(Token.start_ms)
        .join(
            Sentence,
            (Sentence.version_id == Token.version_id) & (Sentence.token_start == Token.idx),
        )
        .where(Sentence.version_id == version_id)
        .order_by(Sentence.ord)
    ).scalars()
    return [int(value) for value in rows]


def build_grid(session: Session, version: TranscriptVersion) -> Grid:
    media = session.get(Media, version.media_id)
    if media is None:
        raise AppError("media_not_found", "找不到对应的音频", status=404)
    boundaries = boundary_grid(sentence_starts(session, version.id), duration_ms=media.duration_ms)
    return Grid(
        boundaries=boundaries,
        targets=snap_targets(boundaries),
        duration_ms=media.duration_ms,
    )


def snap_edge(value_ms: int, grid: Grid, *, radius_ms: int = 120) -> int:
    """Snap one dragged edge. Exposed so the router and tests share one rule."""
    return snap(value_ms, grid.targets, radius_ms=radius_ms)


# --- reads --------------------------------------------------------------


def list_items(session: Session, project_id: str) -> list[VideoItem]:
    return list(
        session.scalars(
            select(VideoItem)
            .where(VideoItem.project_id == project_id)
            .order_by(VideoItem.start_ms, VideoItem.z)
        )
    )


def view_items(
    session: Session, project_id: str, version: TranscriptVersion, keeps: list[tuple[int, int]]
) -> list[ItemView]:
    """Blocks with their *output* length, which is what the UI must show.

    ``orphaned`` is derived from the compiled keep list rather than read off the row,
    because the row's flag is only refreshed on the write paths. Deriving it here means
    a client that asks for the timeline always gets the truth, even if some future
    edit path forgets to call :func:`refresh_after_audio_edit` — and "the block says it
    is fine but plays for zero seconds" is exactly the kind of disagreement that
    produces a bug report nobody can reproduce.
    """
    rows = list_items(session, project_id)
    views: list[ItemView] = []
    for row in rows:
        interval = Interval(row.start_ms, row.end_ms)
        output = kept_ms(interval, keeps)
        views.append(ItemView(item=row, output_ms=output, orphaned=output <= 0))
    return views


def _is_orphaned(row: VideoItem) -> bool:
    """Persisted orphans only: zero length with the flag cleared means "unset"."""
    return row.orphaned or row.end_ms <= row.start_ms


# --- writes -------------------------------------------------------------


def _validate(kind: str, fit: str, motion: str) -> None:
    if kind not in ITEM_KINDS:
        raise AppError("video_item_kind_invalid", f"未知的素材块类型 {kind}")
    if fit not in FITS:
        raise AppError("video_item_fit_invalid", f"未知的填充方式 {fit}")
    if motion not in MOTIONS:
        raise AppError("video_item_motion_invalid", f"未知的运镜方式 {motion}")


def create_item(
    session: Session,
    project_id: str,
    *,
    kind: str,
    start_ms: int,
    end_ms: int,
    fit: str = "blur_fill",
    motion: str = "none",
    z: int = 0,
    asset_id: str | None = None,
    deck_id: str | None = None,
    slide_id: str | None = None,
    gallery_json: str = "[]",
) -> VideoItem:
    _validate(kind, fit, motion)
    if end_ms <= start_ms:
        raise AppError(
            "video_item_empty",
            "素材块时长为 0",
            hint="把两端拖到不同的句子边界上，至少要有半秒。",
        )
    now = utcnow_iso()
    row = VideoItem(
        id=new_id("vitem"),
        project_id=project_id,
        kind=kind,
        start_ms=start_ms,
        end_ms=end_ms,
        z=z,
        fit=fit,
        motion=motion,
        asset_id=asset_id,
        deck_id=deck_id,
        slide_id=slide_id,
        gallery_json=gallery_json,
        orphaned=False,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def update_item(
    session: Session,
    item_id: str,
    *,
    kind: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    fit: str | None = None,
    motion: str | None = None,
    z: int | None = None,
    asset_id: str | None = None,
    deck_id: str | None = None,
    slide_id: str | None = None,
    gallery_json: str | None = None,
) -> VideoItem:
    """Patch a block. ``None`` means "leave alone", so a cleared field must pass ``""``."""
    row = session.get(VideoItem, item_id)
    if row is None:
        raise AppError("video_item_not_found", "素材块不存在", status=404)

    new_kind = kind if kind is not None else row.kind
    new_fit = fit if fit is not None else row.fit
    new_motion = motion if motion is not None else row.motion
    _validate(new_kind, new_fit, new_motion)

    row.kind = new_kind
    row.fit = new_fit
    row.motion = new_motion
    if z is not None:
        row.z = z
    if asset_id is not None:
        row.asset_id = asset_id or None
    if deck_id is not None:
        row.deck_id = deck_id or None
    if slide_id is not None:
        row.slide_id = slide_id or None
    if gallery_json is not None:
        row.gallery_json = gallery_json

    new_start = start_ms if start_ms is not None else row.start_ms
    new_end = end_ms if end_ms is not None else row.end_ms
    if new_end <= new_start:
        raise AppError("video_item_empty", "素材块时长为 0", hint="两端至少要隔一个句子。")
    # Moving the block by hand is the user answering the orphan prompt; keeping
    # the flag set would make it look broken forever.
    if start_ms is not None or end_ms is not None:
        row.orphaned = False
    row.start_ms = new_start
    row.end_ms = new_end
    row.updated_at = utcnow_iso()
    session.flush()
    return row


def delete_item(session: Session, item_id: str) -> None:
    row = session.get(VideoItem, item_id)
    if row is None:
        raise AppError("video_item_not_found", "素材块不存在", status=404)
    session.delete(row)
    session.flush()


# --- keeping anchors honest after an audio edit -------------------------


def project_has_items(session: Session, project_id: str) -> bool:
    """Cheap guard: the audio-edit hot paths call this before doing any work."""
    return (
        session.scalars(select(VideoItem.id).where(VideoItem.project_id == project_id).limit(1)).first()
        is not None
    )


def refresh_anchors(session: Session, version: TranscriptVersion) -> int:
    """Re-seat block edges whose sentence boundary no longer exists.

    Call after anything that can change the set of sentence starts: a boundary
    edit, a merge, or a deletion that swallowed a whole sentence. Edges still on a
    surviving boundary do not move, so the common case — deleting audio *inside* a
    block — costs one query and changes nothing.

    Returns how many blocks actually moved, which the caller reports to the user.
    """
    if not project_has_items(session, version.project_id):
        return 0

    grid = build_grid(session, version)
    rows = list_items(session, version.project_id)
    before = [(row.start_ms, row.end_ms) for row in rows]
    after = renormalize(
        [Interval(start, end) for start, end in before],
        grid.boundaries,
        duration_ms=grid.duration_ms,
    )

    moved = 0
    for row, (start, end), interval in zip(rows, before, after, strict=True):
        if (interval.start_ms, interval.end_ms) != (start, end):
            row.start_ms = interval.start_ms
            row.end_ms = interval.end_ms
            row.updated_at = utcnow_iso()
            moved += 1
    if moved:
        session.flush()
        log.info("video.anchors_moved", version=version.id, moved=moved)
    return moved


def mark_orphans(
    session: Session,
    version: TranscriptVersion,
    keeps: list[tuple[int, int]],
) -> list[VideoItem]:
    """Flag blocks whose audio was deleted in full.

    Measured on real edits this happens to 7.54 % of sentences in a 1074-sentence
    project, so it is not exotic. The row stays: the user may restore that
    sentence tomorrow, and deleting their clip on their behalf is not ours to do.
    """
    rows = list_items(session, version.project_id)
    doomed = orphaned_blocks([Interval(row.start_ms, row.end_ms) for row in rows], keeps)
    flagged: list[VideoItem] = []
    for index in doomed:
        row = rows[index]
        if not row.orphaned:
            row.orphaned = True
            row.updated_at = utcnow_iso()
            flagged.append(row)
    for index, row in enumerate(rows):
        if index not in doomed and row.orphaned:
            row.orphaned = False
            row.updated_at = utcnow_iso()
    session.flush()
    return flagged


def shift_items(session: Session, project_id: str, delta_ms: int) -> int:
    """Rigid whole-timeline move, for ``align.apply_shift``.

    The shift already moves every token and deletion by the same delta, so moving
    the video rows by it keeps everything coherent — and unlike a renormalize, it
    preserves edges that deliberately sat between sentence starts.
    """
    if delta_ms == 0:
        return 0
    rows = list_items(session, project_id)
    now = utcnow_iso()
    for row in rows:
        row.start_ms += delta_ms
        row.end_ms += delta_ms
        row.updated_at = now
    session.flush()
    return len(rows)


@dataclass(frozen=True)
class AnchorReport:
    """What an audio edit did to the video track, in the user's terms."""

    moved: int = 0
    orphaned: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.moved or self.orphaned)

    def message(self) -> str:
        """One line for the notice bar, or ``""`` when nothing happened.

        Silence on the common case is deliberate: deleting a word inside a block
        moves nothing, and a notice saying 「0 个素材块受影响」 on every keystroke is
        noise that trains the user to ignore the bar.
        """
        parts: list[str] = []
        if self.moved:
            parts.append(f"{self.moved} 个素材块边界已跟随吸附")
        if self.orphaned:
            parts.append(f"{self.orphaned} 个素材块对应的音频已删除")
        return " · ".join(parts)

    def as_dict(self) -> dict[str, object]:
        return {"moved": self.moved, "orphaned": self.orphaned, "message": self.message()}


def refresh_after_audio_edit(
    session: Session,
    settings: Settings,
    version_id: str,
) -> AnchorReport:
    """Keep the video track coherent after any audio edit.

    Called from the three places that can change what the sentence grid looks like
    (deletion, restore, boundary edit) and from undo. One function for all of them:
    each entry point that re-seats anchors separately is a chance for the rules to
    drift apart.

    Cheap in the common case — if the project has no video blocks, or the compiled
    keep list still leaves every edge on a boundary, this does one `SELECT` and
    returns zeros.
    """
    version = session.get(TranscriptVersion, version_id)
    if version is None or not project_has_items(session, version.project_id):
        return AnchorReport()

    # Imported here: ``clipping`` imports nothing from this module today, and a
    # module-level import would make the dependency circular the day it does.
    from animatter.services import clipping as clipping_service

    edl = clipping_service.build_edl(session, settings, version)
    keeps = [(keep.orig_start, keep.orig_end) for keep in edl.keeps]

    flagged = mark_orphans(session, version, keeps)
    moved = refresh_anchors(session, version)
    report = AnchorReport(moved=moved, orphaned=len(flagged))
    if report.changed:
        log.info("video.anchors_refreshed", version=version_id, moved=moved, orphaned=len(flagged))
    return report
