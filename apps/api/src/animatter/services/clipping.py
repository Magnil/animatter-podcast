"""Clipping: deletion set → EDL → rendered audio.

Deletions are stored per transcript version. The EDL is always recomputed from
them rather than persisted, because it is a *derived* artefact: caching it would
let the stored plan drift from the deletions it claims to reflect.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.edl import (
    Deletion,
    Edl,
    EdlError,
    SilenceSpan,
    compile_edl,
    edl_signature,
)
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.models import Deletion as DeletionRow
from animatter.models import Media, Render, TranscriptVersion
from animatter.services import silence_map

log = get_logger(__name__)

#: Searching further than this for silence finds a match in the next sentence,
#: which would delete content the user did not select.
SILENCE_SEARCH_MS = 600


@dataclass(frozen=True)
class EdlView:
    original_duration_ms: int
    output_duration_ms: int
    removed_ms: int
    segment_count: int
    keeps: list[dict[str, int]]
    deletions: list[dict[str, object]]
    clamps: list[dict[str, object]]
    signature: str


def get_version(session: Session, version_id: str) -> TranscriptVersion:
    version = session.get(TranscriptVersion, version_id)
    if version is None:
        raise AppError("version_not_found", "转写版本不存在", hint="刷新版本列表。", status=404)
    return version


def get_active_version(session: Session, project_id: str) -> TranscriptVersion:
    """The project's active transcript version.

    Needed by routes that are addressed by ``project_id`` (the video timeline stores
    blocks per project, but every length comes from *a* transcript). Falls back to
    the newest version when nothing is flagged active, because a project that has a
    transcript but a stale ``is_active`` flag should still open.
    """
    version = session.scalars(
        select(TranscriptVersion)
        .where(TranscriptVersion.project_id == project_id, TranscriptVersion.is_active.is_(True))
        .order_by(TranscriptVersion.version.desc())
    ).first()
    if version is not None:
        return version

    latest = session.scalars(
        select(TranscriptVersion)
        .where(TranscriptVersion.project_id == project_id)
        .order_by(TranscriptVersion.version.desc())
    ).first()
    if latest is None:
        raise AppError(
            "version_not_found",
            "这个项目还没有转写结果",
            hint="先完成一次转写，再剪视频。",
            status=404,
        )
    return latest


def list_deletions(session: Session, version_id: str) -> list[DeletionRow]:
    return list(
        session.scalars(
            select(DeletionRow)
            .where(DeletionRow.version_id == version_id, DeletionRow.removed.is_(False))
            .order_by(DeletionRow.start_ms)
        )
    )


def add_deletion(
    session: Session,
    version_id: str,
    *,
    start_ms: int,
    end_ms: int,
    source: str = "range",
    token_ids: list[int] | None = None,
) -> DeletionRow:
    get_version(session, version_id)
    if end_ms <= start_ms:
        raise AppError("deletion_empty", "删除区间为空", hint="至少选择一个字或一段波形。")

    row = DeletionRow(
        id=new_id("del"),
        version_id=version_id,
        start_ms=max(0, start_ms),
        end_ms=end_ms,
        source=source,
        token_ids_json=json.dumps(token_ids or []),
        created_at=utcnow_iso(),
        removed=False,
    )
    session.add(row)
    session.flush()
    log.info("deletion.added", version=version_id, start=start_ms, end=end_ms, source=source)
    return row


def add_token_deletion(session: Session, version_id: str, token_ids: list[int]) -> list[DeletionRow]:
    """Delete the given tokens, coalescing contiguous runs into one range.

    Deleting a whole sentence must be one deletion, not forty: segment count is
    what makes rendering slow and the filtergraph large.
    """
    from animatter.services import transcribe as transcribe_service

    if not token_ids:
        raise AppError("deletion_empty", "没有选中任何字")

    tokens = transcribe_service.load_tokens(session, version_id)
    wanted = sorted({index for index in token_ids if 0 <= index < len(tokens)})
    if not wanted:
        raise AppError("deletion_empty", "选中的位置不在转录范围内")

    runs: list[list[int]] = [[wanted[0]]]
    for index in wanted[1:]:
        if index == runs[-1][-1] + 1:
            runs[-1].append(index)
        else:
            runs.append([index])

    rows: list[DeletionRow] = []
    for run in runs:
        first = tokens[run[0]]
        last = tokens[run[-1]]
        rows.append(
            add_deletion(
                session,
                version_id,
                start_ms=first.start_ms,
                end_ms=last.end_ms,
                source="token",
                token_ids=run,
            )
        )
    return rows


def restore_token_deletion(session: Session, version_id: str, token_ids: list[int]) -> list[DeletionRow]:
    """Take back the deletion of the given tokens — the 「还」 button.

    Restores exactly the tokens asked for, not every deletion that happens to
    overlap them. A deletion row can cover more than one word (a sentence delete,
    a waveform range), and undoing the whole row because one of its words was
    clicked would resurrect audio the user did not point at.

    When a row covers more than the requested words, it is replaced by what is
    left: the requested tokens become keepable again, the rest stays deleted. That
    is a *narrowing* of a soft delete, so the original row is marked removed and
    the remainder is written as a fresh row — the history then shows one taken
    back and one still in effect, which is the truth.
    """
    from animatter.services import transcribe as transcribe_service

    if not token_ids:
        raise AppError("deletion_empty", "没有选中任何字")

    tokens = transcribe_service.load_tokens(session, version_id)
    wanted = sorted({index for index in token_ids if 0 <= index < len(tokens)})
    if not wanted:
        raise AppError("deletion_empty", "选中的位置不在转录范围内")

    rows = list_deletions(session, version_id)
    restored: list[DeletionRow] = []

    for row in rows:
        covered = {int(value) for value in json.loads(row.token_ids_json or "[]")}
        hit = covered & set(wanted)
        if not hit:
            continue

        row.removed = True
        restored.append(row)
        # What is left of this cut: the same span minus the words being taken
        # back. Words are the unit; a row with no token ids is a waveform range
        # and cannot be split by word, so it is restored whole.
        remaining = [idx for idx in sorted(covered) if idx not in hit]
        if not remaining:
            continue

        runs: list[list[int]] = [[remaining[0]]]
        for index in remaining[1:]:
            if index == runs[-1][-1] + 1:
                runs[-1].append(index)
            else:
                runs.append([index])
        for run in runs:
            add_deletion(
                session,
                version_id,
                start_ms=tokens[run[0]].start_ms,
                end_ms=tokens[run[-1]].end_ms,
                source=row.source,
                token_ids=run,
            )

    session.flush()
    log.info("deletion.restored", version=version_id, rows=len(restored), words=len(wanted))
    return restored


def remove_deletion(session: Session, deletion_id: str) -> None:
    row = session.get(DeletionRow, deletion_id)
    if row is None:
        raise AppError("deletion_not_found", "该删除记录不存在", status=404)
    # Soft delete: undo history and the UI both want to show what was removed.
    row.removed = True
    session.flush()


def load_silences(
    session: Session, settings: Settings, version: TranscriptVersion, media: Media
) -> list[SilenceSpan]:
    """Silence spans for this media, from the shared cache.

    This used to call ``ffmpeg silencedetect`` on the whole file every time, and
    ``build_edl`` runs on every edit — 9.1 s per call on a 105-minute episode,
    on the thread that has to answer the keystroke. The cache is keyed by
    content hash plus detector parameters, so it also stops "moving the cleanup
    slider" from changing which edges a drag snaps to.
    """
    return silence_map.for_media(session, settings, version, media)


def build_edl(
    session: Session,
    settings: Settings,
    version: TranscriptVersion,
    *,
    snap: bool = True,
) -> Edl:
    """Compile the current deletion set into a plan."""
    media = session.get(Media, version.media_id)
    if media is None:
        raise AppError("media_not_found", "找不到对应的音频", status=404)

    rows = list_deletions(session, version.id)
    deletions = [
        Deletion(
            start_ms=row.start_ms,
            end_ms=row.end_ms,
            source=row.source,
            token_ids=tuple(json.loads(row.token_ids_json or "[]")),
        )
        for row in rows
    ]

    silences: list[SilenceSpan] = []
    if snap and deletions:
        silences = load_silences(session, settings, version, media)
        # Only silences near a cut point matter; snapping searches by distance,
        # but a 3h file has thousands of spans and 400 deletions × 1814 spans is
        # the kind of loop that shows up as a stutter on every edit.
        silences = [
            span
            for span in silences
            if any(
                abs(span.midpoint - d.start_ms) <= SILENCE_SEARCH_MS
                or abs(span.midpoint - d.end_ms) <= SILENCE_SEARCH_MS
                for d in deletions
            )
        ]

    try:
        return compile_edl(
            original_duration_ms=media.duration_ms,
            deletions=deletions,
            silences=silences,
            snap_to_silences=snap,
        )
    except EdlError as exc:
        raise AppError(
            "edl_invalid",
            str(exc),
            hint="取消一部分删除，或减少删除区间。",
        ) from exc


def view(edl: Edl) -> EdlView:
    return EdlView(
        original_duration_ms=edl.original_duration_ms,
        output_duration_ms=edl.output_duration_ms,
        removed_ms=edl.removed_ms,
        segment_count=edl.segment_count,
        keeps=[
            {"orig_start": k.orig_start, "orig_end": k.orig_end, "new_start": k.new_start} for k in edl.keeps
        ],
        deletions=[{"start_ms": d.start_ms, "end_ms": d.end_ms, "source": d.source} for d in edl.deletions],
        clamps=[
            {"requested": c.requested, "applied": c.applied, "edge": c.edge, "reason": c.reason}
            for c in edl.clamps
        ],
        signature=edl_signature(edl),
    )


def list_renders(session: Session, version_id: str) -> list[Render]:
    return list(
        session.scalars(
            select(Render).where(Render.version_id == version_id).order_by(Render.created_at.desc())
        )
    )
