"""Background rendering.

Why this exists
---------------
``POST /versions/{id}/render`` used to run ffmpeg inside the request, holding a
SQLite write transaction for the whole encode. On a 105-minute episode that is
**twelve minutes** of wall clock, during which:

* the browser shows a spinner with no progress, which the user reported as
  「导出成片 点击后无反应」 — the button did work, it just never said anything;
* every other write in the app fails with ``database is locked``, because one
  uncommitted write transaction in SQLite blocks all the others. That was a real
  500 in this session, not a theoretical concern.

So the render is split into: a short transaction that records the job and
commits, ffmpeg running off the request thread, and short transactions that only
write progress and the final status. Nothing holds the write lock while ffmpeg
works.

One render per version at a time
--------------------------------
Two concurrent renders of the same version would write the same output path and
interleave into a corrupt file. The per-version lock is process-local and that is
sufficient: this is a single-user local app with one API process.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.edl import edl_signature
from animatter.db import Database
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.media import render as render_media
from animatter.models import Media, Render, TranscriptVersion
from animatter.services import clipping as clipping_service
from animatter.services import projects as project_service

log = get_logger(__name__)

#: Rendering is CPU-bound and one encode already saturates what ffmpeg will use.
#: Two slots cover "export while previewing another episode" without letting a
#: burst of clicks start a dozen encodes.
_MAX_WORKERS = 2

_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="render")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_futures: dict[str, Future[None]] = {}

_ACTIVE = ("queued", "running")


def _lock_for(version_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(version_id, threading.Lock())


@dataclass(frozen=True)
class StartedRender:
    render: Render
    #: ``True`` when an identical EDL is already rendered, or already queued and
    #: being encoded right now. The client should then poll rather than assume a
    #: fresh job, which is the same thing it does either way.
    reused: bool


def active_for_version(session: Session, version_id: str) -> Render | None:
    """The render currently queued or running for this version, if any."""
    return session.scalars(
        select(Render)
        .where(Render.version_id == version_id, Render.status.in_(_ACTIVE))
        .order_by(Render.created_at.desc())
    ).first()


def get(session: Session, render_id: str) -> Render:
    row = session.get(Render, render_id)
    if row is None:
        raise AppError("render_not_found", "渲染任务不存在", status=404)
    return row


def start(
    db: Database,
    settings: Settings,
    version_id: str,
    *,
    preview: bool = False,
    lossless: bool = False,
    window: tuple[int, int] | None = None,
) -> StartedRender:
    """Record the job and hand it to the worker pool.

    The request returns as soon as the row is committed; from then on the client
    polls ``GET /api/renders/{id}``.
    """
    with db.session() as session:
        version = clipping_service.get_version(session, version_id)
        edl = clipping_service.build_edl(session, settings, version)
        signature = edl_signature(edl)

        if not preview:
            existing = session.scalars(
                select(Render)
                .where(
                    Render.version_id == version_id,
                    Render.edl_hash == signature,
                    Render.status == "done",
                )
                .order_by(Render.created_at.desc())
            ).first()
            if existing is not None and Path(existing.out_path).exists():
                log.info("render.cached", render_id=existing.id, signature=signature)
                return StartedRender(render=existing, reused=True)

        # A second click while the first encode is still running must attach to
        # that encode, not start a competing one writing the same file.
        running = active_for_version(session, version_id)
        if running is not None and running.edl_hash == signature:
            return StartedRender(render=running, reused=True)

        record = Render(
            id=new_id("rnd"),
            project_id=version.project_id,
            version_id=version_id,
            edl_hash=signature,
            out_path="",  # written by the worker, once the target path is known
            duration_ms=edl.output_duration_ms,
            status="queued",
            progress=0.0,
            created_at=utcnow_iso(),
        )
        session.add(record)
        # Commit before the worker starts: it opens its own session and must be
        # able to see this row.
        session.commit()
        render_id = record.id
        log.info("render.queued", render_id=render_id, signature=signature)

    _futures[render_id] = _executor.submit(
        _run, db, settings, render_id, version_id, preview, lossless, window
    )
    with db.session() as session:
        started = session.get(Render, render_id)
        assert started is not None
        return StartedRender(render=started, reused=False)


def _run(
    db: Database,
    settings: Settings,
    render_id: str,
    version_id: str,
    preview: bool,
    lossless: bool,
    window: tuple[int, int] | None,
) -> None:
    try:
        with _lock_for(version_id):
            _encode(db, settings, render_id, version_id, preview, lossless, window)
    except AppError as exc:
        _finish(db, render_id, status="failed", error=f"{exc.message}：{exc.hint}")
    except Exception as exc:
        # A worker thread must never die silently: the row would stay "running"
        # and the UI would poll it forever.
        log.exception("render.crashed", render_id=render_id)
        _finish(db, render_id, status="failed", error=str(exc))
    finally:
        with _locks_guard:
            _futures.pop(render_id, None)


def _encode(
    db: Database,
    settings: Settings,
    render_id: str,
    version_id: str,
    preview: bool,
    lossless: bool,
    window: tuple[int, int] | None,
) -> None:
    with db.session() as session:
        record = session.get(Render, render_id)
        if record is None:
            raise AppError("render_not_found", "渲染任务不存在", status=404)
        version = session.get(TranscriptVersion, version_id)
        if version is None:
            raise AppError("version_not_found", "转写版本不存在", status=404)
        media = session.get(Media, version.media_id)
        if media is None:
            raise AppError("media_not_found", "找不到对应的音频", status=404)
        edl = clipping_service.build_edl(session, settings, version)
        out_dir = project_service.project_root(settings, version.project_id) / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".wav" if lossless else ".m4a"
        stem = f"{version_id}-{record.edl_hash[:12]}{'-preview' if preview else ''}"
        out_path = out_dir / f"{stem}{suffix}"
        record.out_path = str(out_path)
        record.status = "running"
        record.progress = 0.0
        session.flush()
        sample_rate = media.sample_rate or 44_100
        channels = media.channels or 2
        source = Path(media.path)
        original_duration_ms = edl.original_duration_ms

    def report(fraction: float) -> None:
        # A separate short transaction per update: the point is that no
        # transaction is open while ffmpeg runs.
        with db.session() as progress_session:
            row = progress_session.get(Render, render_id)
            if row is not None and row.status == "running":
                row.progress = round(fraction, 4)

    try:
        if preview:
            span = window or (0, min(60_000, original_duration_ms))
            result = render_media.render_preview(
                settings.ffmpeg_path,
                source=source,
                edl=edl,
                target=out_path,
                sample_rate=sample_rate,
                channels=channels,
                window_orig=span,
            )
        else:
            result = render_media.render(
                settings.ffmpeg_path,
                source=source,
                edl=edl,
                target=out_path,
                sample_rate=sample_rate,
                channels=channels,
                lossless=lossless,
                progress=report,
            )
    except AppError as exc:
        _finish(db, render_id, status="failed", error=f"{exc.message}：{exc.hint}")
        return

    _finish(db, render_id, status="done", duration_ms=result.duration_ms)
    log.info("render.recorded", render_id=render_id, duration_ms=result.duration_ms)


def _finish(
    db: Database,
    render_id: str,
    *,
    status: str,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    with db.session() as session:
        row = session.get(Render, render_id)
        if row is None:
            return
        row.status = status
        row.error = error
        if status == "done":
            row.progress = 1.0
        if duration_ms is not None:
            row.duration_ms = duration_ms


def fail_orphaned(db: Database) -> int:
    """Mark rows left ``queued``/``running`` by a previous process as failed.

    Nothing survives a restart: the thread that owned the encode is gone and the
    half-written file is unusable. Without this sweep the UI polls a job that can
    never finish.
    """
    with db.session() as session:
        rows = session.scalars(select(Render).where(Render.status.in_(_ACTIVE))).all()
        for row in rows:
            row.status = "failed"
            row.error = "服务重启，渲染中断；请重新导出。"
        count = len(rows)
    if count:
        log.warning("render.orphans_failed", count=count)
    return count
