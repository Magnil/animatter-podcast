"""Clipping: deletion set → EDL → rendered audio.

Deletions are stored per transcript version. The EDL is always recomputed from
them rather than persisted, because it is a *derived* artefact: caching it would
let the stored plan drift from the deletions it claims to reflect.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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
from animatter.media import ffmpeg, render
from animatter.models import Deletion as DeletionRow
from animatter.models import Media, Render, TranscriptVersion
from animatter.services import projects as project_service

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


def remove_deletion(session: Session, deletion_id: str) -> None:
    row = session.get(DeletionRow, deletion_id)
    if row is None:
        raise AppError("deletion_not_found", "该删除记录不存在", status=404)
    # Soft delete: undo history and the UI both want to show what was removed.
    row.removed = True
    session.flush()


def load_silences(settings: Settings, project_id: str, media: Media) -> list[SilenceSpan]:
    root = project_service.project_root(settings, project_id)
    spans = ffmpeg.detect_silence(settings.ffmpeg_path, Path(media.path))
    log.info("clipping.silences", project=project_id, count=len(spans), root=str(root))
    return [
        SilenceSpan(start_ms=span.start_ms, end_ms=span.end_ms) for span in spans if span.duration_ms >= 120
    ]


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
        silences = load_silences(settings, version.project_id, media)
        # Only silences near a cut point matter; passing the whole list is fine
        # because snapping searches by distance, but a 3h file can have thousands.
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


def render_version(
    session: Session,
    settings: Settings,
    version_id: str,
    *,
    preview: bool = False,
    window: tuple[int, int] | None = None,
    lossless: bool = False,
    progress: object = None,
) -> Render:
    """Render the current EDL and record it.

    A render is identified by its EDL signature so an unchanged plan reuses the
    previous file instead of re-encoding a two-hour episode.
    """
    version = get_version(session, version_id)
    media = session.get(Media, version.media_id)
    if media is None:
        raise AppError("media_not_found", "找不到对应的音频", status=404)

    edl = build_edl(session, settings, version)
    signature = edl_signature(edl)

    existing = session.scalars(
        select(Render)
        .where(Render.version_id == version_id, Render.edl_hash == signature, Render.status == "done")
        .order_by(Render.created_at.desc())
    ).first()
    if existing is not None and Path(existing.out_path).exists() and not preview:
        log.info("render.cached", render_id=existing.id, signature=signature)
        return existing

    root = project_service.project_root(settings, version.project_id)
    out_dir = root / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".wav" if lossless else ".m4a"
    out_path = out_dir / f"{version_id}-{signature[:12]}{'-preview' if preview else ''}{suffix}"

    record = Render(
        id=new_id("rnd"),
        project_id=version.project_id,
        version_id=version_id,
        edl_hash=signature,
        out_path=str(out_path),
        duration_ms=0,
        status="running",
        created_at=utcnow_iso(),
    )
    session.add(record)
    session.flush()

    try:
        if preview:
            span = window or (0, min(60_000, media.duration_ms))
            result = render.render_preview(
                settings.ffmpeg_path,
                source=Path(media.path),
                edl=edl,
                target=out_path,
                sample_rate=media.sample_rate or 44_100,
                channels=media.channels or 2,
                window_orig=span,
            )
        else:
            result = render.render(
                settings.ffmpeg_path,
                source=Path(media.path),
                edl=edl,
                target=out_path,
                sample_rate=media.sample_rate or 44_100,
                channels=media.channels or 2,
                lossless=lossless,
            )
    except AppError:
        record.status = "failed"
        session.flush()
        raise

    record.status = "done"
    record.duration_ms = result.duration_ms
    session.flush()
    log.info("render.recorded", render_id=record.id, duration_ms=result.duration_ms)
    return record


def list_renders(session: Session, version_id: str) -> list[Render]:
    return list(
        session.scalars(
            select(Render).where(Render.version_id == version_id).order_by(Render.created_at.desc())
        )
    )
