"""Video export and still preview (§19.5, §19.7).

The export is a background job for the same reason the audio render is: a one-hour
film takes ~14 minutes of encoding, and holding a SQLite write transaction for that
long makes every other write in the app fail with ``database is locked``. The
pattern is copied from ``services/renders.py`` rather than shared with it, because
the two have different inputs (an EDL vs an EDL *plus* a template and a timeline) and
different progress denominators; a common base class would only add indirection.

Order of work, and why:

1. **Audio first.** If there is no rendered audio, there is nothing to mux, and
   discovering that after fourteen minutes of x264 is unacceptable. The audio render
   is reused when an identical EDL was already exported — it is keyed by EDL hash.
2. **Cues → ASS.** Cues are built in *output* time (mapped through the TimeMap), so
   subtitles cannot drift away from the cut speech.
3. **Silent video.** One ffmpeg, one filtergraph in a script file (§19.11.1).
4. **Mux.** ``-c copy``; measured 0.24 s per 60 s.

Still preview is a separate, request-time path: ~200 ms per frame, so the UI can call
it as the playhead moves without a job row.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.edl import Edl
from animatter.errors import AppError
from animatter.ids import new_id, utcnow_iso
from animatter.log import get_logger
from animatter.media import video as video_media
from animatter.media.subtitle_ass import AssCue, SubtitleStyle, write_ass
from animatter.models import Render, TranscriptVersion, VideoItem, VideoTemplate
from animatter.services import clipping as clipping_service
from animatter.services import projects as project_service
from animatter.services import subtitles as subtitle_service
from animatter.services import video as video_service
from animatter.services import video_templates as template_service

log = get_logger(__name__)

#: Two slots, same reasoning as the audio renderer: one encode already saturates
#: ffmpeg, and two covers "export while checking another episode".
_MAX_WORKERS = 2

_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="video")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_futures: dict[str, Future[None]] = {}

_ACTIVE = ("queued", "running")


def _lock_for(project_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(project_id, threading.Lock())


@dataclass(frozen=True)
class ExportOptions:
    """Everything the 「导出视频」 dialog can change."""

    #: ``None`` means "use the defaults"; a mutable-ish instance as a default would
    #: be shared across calls, and RUF009 is right to reject one.
    subtitle_style: SubtitleStyle | None = None
    burn_subtitles: bool = True
    lossless_audio: bool = False

    def style(self) -> SubtitleStyle:
        return self.subtitle_style or SubtitleStyle()


@dataclass(frozen=True)
class PreparedVideo:
    """The inputs an encode needs, resolved once so the worker never re-reads them."""

    template: VideoTemplate
    box_rect: dict[str, int]
    items: list[VideoItem]
    edl: Edl
    audio_path: Path
    duration_ms: int


def output_dir(settings: Settings, project_id: str) -> Path:
    path = project_service.project_root(settings, project_id) / "video" / "out"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- preparing ----------------------------------------------------------


def _audio_for(
    session: Session, settings: Settings, version: TranscriptVersion, *, lossless: bool
) -> Path:
    """The already-rendered audio for this exact EDL, or a fresh render of it.

    Reusing the audio is the whole point of the ``-c copy`` mux: re-encoding it here
    would cost another ten-plus minutes and produce a file the user has not approved.
    """
    from animatter.core.edl import edl_signature

    edl = clipping_service.build_edl(session, settings, version)
    signature = edl_signature(edl)
    candidates = session.scalars(
        select(Render)
        .where(Render.version_id == version.id, Render.status == "done")
        .order_by(Render.created_at.desc())
    ).all()
    for row in candidates:
        if row.edl_hash != signature:
            continue
        path = Path(row.out_path)
        if not path.is_file():
            continue
        if lossless and path.suffix != ".wav":
            continue
        if not lossless and path.suffix == ".wav":
            # A WAV can still be muxed, but it is 10× the size; prefer the m4a.
            continue
        return path

    raise AppError(
        "audio_not_rendered",
        "还没有这个版本的成片音频",
        hint="先在「导出」里导出成片音频，再导出视频。视频会直接复用那条音轨。",
        status=409,
    )


def prepare(
    session: Session,
    settings: Settings,
    project_id: str,
    version_id: str,
    *,
    lossless_audio: bool = False,
) -> PreparedVideo:
    """Resolve template, timeline, EDL and audio — or raise a usable error."""
    version = clipping_service.get_version(session, version_id)
    template = template_service.require(session, project_id)
    if template.status != "ready":
        raise AppError(
            "template_not_ready",
            "模板还没编译好",
            hint="等编译完成，或者重新导入这份 PSD。",
            status=409,
        )

    edl = clipping_service.build_edl(session, settings, version)
    audio = _audio_for(session, settings, version, lossless=lossless_audio)
    items = video_service.list_items(session, project_id)

    return PreparedVideo(
        template=template,
        box_rect=template_service.box_rect_of(template),
        items=items,
        edl=edl,
        audio_path=audio,
        duration_ms=edl.output_duration_ms,
    )


def _ass_for(
    session: Session,
    version_id: str,
    edl: Edl,
    style: SubtitleStyle,
    target: Path,
) -> Path:
    """Build the burned-in subtitle file in output time."""
    time_map = edl.time_map() if edl.removed_ms else None
    cues, _issues = subtitle_service.build_cues(session, version_id, time_map=time_map)
    ass_cues = [AssCue(start_ms=cue.start_ms, end_ms=cue.end_ms, text=cue.text) for cue in cues]
    if not ass_cues:
        raise AppError(
            "no_subtitle_cues",
            "这个版本没有可烧入的字幕",
            hint="先完成转写并确认字幕内容。",
        )
    return write_ass(target, ass_cues, style=style)


def _layers(template: VideoTemplate) -> list[video_media.Layer]:
    """Static overlays above the box content, in P1 just the frame stroke."""
    path = Path(template.overlay_path) if template.overlay_path else None
    if path is None or not path.is_file():
        return []
    return [video_media.Layer(path=path, x=0, y=0)]


# --- still preview ------------------------------------------------------


def preview_still(
    session: Session,
    settings: Settings,
    project_id: str,
    version_id: str,
    *,
    at_ms: int,
    style: SubtitleStyle | None = None,
    burn_subtitles: bool = True,
) -> Path:
    """Composite one frame at ``at_ms`` on the output timeline.

    Request-time, not a job: a still costs ~200 ms, which is the entire reason v1
    promises a still rather than real-time playback. The user drags the playhead, lets
    go, and sees the real layout — from the same filtergraph the export uses, so what
    they approve is what they get.
    """
    version = clipping_service.get_version(session, version_id)
    template = template_service.require(session, project_id)
    if template.status != "ready":
        raise AppError("template_not_ready", "模板还没编译好", status=409)

    edl = clipping_service.build_edl(session, settings, version)
    out_dir = output_dir(settings, project_id) / "previews"
    out_dir.mkdir(parents=True, exist_ok=True)

    subtitles: Path | None = None
    if burn_subtitles:
        subtitles = _ass_for(
            session, version_id, edl, style or SubtitleStyle(), out_dir / "preview.ass"
        )

    target = out_dir / f"still-{at_ms:09d}.png"
    video_media.render_still(
        settings.ffmpeg_path,
        target=target,
        at_ms=int(at_ms),
        width=template.canvas_w or 1920,
        height=template.canvas_h or 1080,
        background=Path(template.background_path),
        layers=_layers(template),
        subtitles=subtitles,
    )
    return target


# --- export -------------------------------------------------------------


def start_export(
    db: object,
    settings: Settings,
    project_id: str,
    version_id: str,
    *,
    options: ExportOptions | None = None,
) -> Render:
    """Record the job and hand it to the worker pool.

    Returns the same ``Render`` row type the audio exporter uses, so the UI polls one
    endpoint shape for both and the progress bar code is not duplicated.
    """
    from animatter.db import Database

    assert isinstance(db, Database)
    resolved = options or ExportOptions()

    with db.session() as session:
        prepared = prepare(session, settings, project_id, version_id)
        record = Render(
            id=new_id("vren"),
            project_id=project_id,
            version_id=version_id,
            # The video's identity is its EDL *and* its timeline *and* its template;
            # hashing only the EDL would let a cached export survive a block move.
            edl_hash=_video_signature(prepared, resolved),
            out_path="",
            duration_ms=prepared.duration_ms,
            status="queued",
            progress=0.0,
            created_at=utcnow_iso(),
        )
        session.add(record)
        session.commit()
        render_id = record.id

    _futures[render_id] = _executor.submit(
        _run, db, settings, render_id, project_id, version_id, resolved
    )
    with db.session() as session:
        started = session.get(Render, render_id)
        assert started is not None
        log.info("video.queued", render_id=render_id, duration_ms=started.duration_ms)
        return started


def _video_signature(prepared: PreparedVideo, options: ExportOptions) -> str:
    """A hash over everything that can change the pixels or the audio."""
    import hashlib

    style = options.style()
    parts = [
        prepared.template.source_sha256,
        prepared.template.box_rect,
        str(prepared.duration_ms),
        hashlib.sha256(
            ";".join(
                f"{item.start_ms}-{item.end_ms}:{item.kind}:{item.fit}:{item.asset_id or ''}"
                for item in prepared.items
            ).encode()
        ).hexdigest(),
        f"{style.font_name}/{style.font_size}/{style.primary_colour}/{options.burn_subtitles}",
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _run(
    db: object,
    settings: Settings,
    render_id: str,
    project_id: str,
    version_id: str,
    options: ExportOptions,
) -> None:
    from animatter.db import Database

    assert isinstance(db, Database)
    try:
        with _lock_for(project_id):
            _encode(db, settings, render_id, project_id, version_id, options)
    except AppError as exc:
        _finish(db, render_id, status="failed", error=f"{exc.message}：{exc.hint}")
    except Exception as exc:
        log.exception("video.crashed", render_id=render_id)
        _finish(db, render_id, status="failed", error=str(exc))
    finally:
        with _locks_guard:
            _futures.pop(render_id, None)


def _encode(
    db: object,
    settings: Settings,
    render_id: str,
    project_id: str,
    version_id: str,
    options: ExportOptions,
) -> None:
    from animatter.db import Database

    assert isinstance(db, Database)

    with db.session() as session:
        prepared = prepare(
            session, settings, project_id, version_id, lossless_audio=options.lossless_audio
        )
        record = session.get(Render, render_id)
        if record is None:
            raise AppError("render_not_found", "导出任务不存在", status=404)

        out = output_dir(settings, project_id)
        stem = f"{version_id}-{record.edl_hash[:12]}"
        silent = out / f"{stem}-silent.mp4"
        final = out / f"{stem}.mp4"
        record.out_path = str(final)
        record.status = "running"
        record.progress = 0.0

    def report(fraction: float) -> None:
        with db.session() as progress_session:
            row = progress_session.get(Render, render_id)
            if row is not None and row.status == "running":
                # Encoding is ~75 % of the wall clock and muxing is ~1 %, but the
                # split is not fixed; scaling to 0.9 keeps the bar from sitting at
                # 100 % while the mux runs.
                row.progress = round(min(0.9, fraction * 0.9), 4)

    subtitles: Path | None = None
    if options.burn_subtitles:
        with db.session() as session:
            subtitles = _ass_for(
                session, version_id, prepared.edl, options.style(), out / f"{stem}.ass"
            )

    video_media.render(
        settings.ffmpeg_path,
        target=silent,
        duration_ms=prepared.duration_ms,
        width=prepared.template.canvas_w or 1920,
        height=prepared.template.canvas_h or 1080,
        background=Path(prepared.template.background_path),
        layers=_layers(prepared.template),
        subtitles=subtitles,
    )
    report(0.95)

    result = video_media.mux(
        settings.ffmpeg_path, video=silent, audio=prepared.audio_path, target=final
    )
    silent.unlink(missing_ok=True)

    _finish(db, render_id, status="done", duration_ms=result.duration_ms or prepared.duration_ms)
    log.info("video.done", render_id=render_id, path=str(final), duration_ms=result.duration_ms)


def _finish(
    db: object,
    render_id: str,
    *,
    status: str,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    from animatter.db import Database

    assert isinstance(db, Database)
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


def fail_orphaned(db: object) -> int:
    """Mark video exports left running by a previous process as failed."""
    from animatter.db import Database

    assert isinstance(db, Database)
    with db.session() as session:
        rows = session.scalars(
            select(Render).where(Render.status.in_(_ACTIVE), Render.id.startswith("vren"))
        ).all()
        for row in rows:
            row.status = "failed"
            row.error = "服务重启，视频导出中断；请重新导出。"
        count = len(rows)
    if count:
        log.warning("video.orphans_failed", count=count)
    return count
