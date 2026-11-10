"""Video endpoints: template import/compile, timeline blocks, preview, export.

The template compile is asynchronous (≈6 s on a real PSD) and the export is
asynchronous (≈14 min for an hour), but the *still preview* is not: it takes ~200 ms
and the UI calls it while the user drags the playhead, so putting it behind a job row
would make the one interaction that must feel immediate the slowest one.
"""

from __future__ import annotations

import concurrent.futures
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.db import Database
from animatter.errors import AppError
from animatter.log import get_logger
from animatter.media.subtitle_ass import SubtitleStyle
from animatter.models import Render, VideoItem
from animatter.services import clipping as clipping_service
from animatter.services import video as video_service
from animatter.services import video_render as video_render_service
from animatter.services import video_templates as template_service

router = APIRouter(prefix="/api", tags=["video"])
log = get_logger(__name__)

#: Compiles are small in number and heavy; two at once keeps the disk honest.
_compile_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="vtpl")


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


# --- template -----------------------------------------------------------


class TemplateImportIn(BaseModel):
    path: str = Field(description="本地 PSD 绝对路径")


class TemplateOut(BaseModel):
    id: str
    status: str
    error: str
    canvas_w: int
    canvas_h: int
    box_rect: dict[str, int]
    box_fit_default: str
    background_url: str
    overlay_url: str
    slots: dict[str, object]
    compiled_at: str | None = None


class TemplateOptionsIn(BaseModel):
    box_fit_default: str | None = None


@router.get("/projects/{project_id}/video/template", response_model=TemplateOut | None)
def get_template(
    project_id: str, session: Session = Depends(_session_dependency)
) -> TemplateOut | None:
    """The current template, or ``null`` when the project has none yet.

    ``null`` rather than a 404: "this project has no video template" is the normal
    starting state, not an error, and a 404 would light up the console on every page
    load before the user has touched the feature.
    """
    view = template_service.view(session, project_id)
    return None if view is None else TemplateOut(**view.as_dict())  # type: ignore[arg-type]


@router.post("/projects/{project_id}/video/template", response_model=TemplateOut, status_code=201)
def import_template(
    project_id: str,
    payload: TemplateImportIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> TemplateOut:
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    row = template_service.import_template(session, settings, project_id, Path(payload.path))
    session.commit()

    if row.status == "pending":
        _compile_pool.submit(_compile_in_background, db, row.id)
    view = template_service.view(session, project_id)
    assert view is not None
    return TemplateOut(**view.as_dict())  # type: ignore[arg-type]


def _compile_in_background(db: Database, template_id: str) -> None:
    """Compile outside the request. Failures land on the row, never on the thread."""
    try:
        with db.session() as session:
            template_service.compile_now(session, template_id)
    except Exception:
        log.exception("template.worker_crashed", template=template_id)


@router.post("/projects/{project_id}/video/template/recompile", response_model=TemplateOut)
def recompile_template(
    project_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> TemplateOut:
    """Run the compile again from the stored source.

    Exists because a compile can fail for a reason that goes away (a font installed,
    a file unlocked) and re-importing the PSD from the user's folder may no longer be
    possible — the original path is not required, the stored copy is.
    """
    db: Database = request.app.state.db
    row = template_service.require(session, project_id)
    row.status = "pending"
    row.error = ""
    session.commit()
    _compile_pool.submit(_compile_in_background, db, row.id)
    view = template_service.view(session, project_id)
    assert view is not None
    return TemplateOut(**view.as_dict())  # type: ignore[arg-type]


@router.patch("/projects/{project_id}/video/template", response_model=TemplateOut)
def update_template_options(
    project_id: str,
    payload: TemplateOptionsIn,
    session: Session = Depends(_session_dependency),
) -> TemplateOut:
    template_service.update_options(
        session, project_id, box_fit_default=payload.box_fit_default
    )
    view = template_service.view(session, project_id)
    assert view is not None
    return TemplateOut(**view.as_dict())  # type: ignore[arg-type]


@router.get("/projects/{project_id}/video/template/asset/{name}")
def template_asset(
    project_id: str,
    name: str,
    session: Session = Depends(_session_dependency),
) -> FileResponse:
    """Serve one compiled PNG. Only the file name is honoured — see the service."""
    path = template_service.asset_path(session, project_id, name)
    return FileResponse(path, media_type="image/png")


# --- timeline -----------------------------------------------------------


class GridOut(BaseModel):
    boundaries: list[int]
    targets: list[int]
    duration_ms: int


class ItemOut(BaseModel):
    id: str
    kind: str
    start_ms: int
    end_ms: int
    duration_ms: int
    output_ms: int
    z: int
    fit: str
    motion: str
    asset_id: str | None = None
    deck_id: str | None = None
    slide_id: str | None = None
    gallery_json: str
    orphaned: bool


class TimelineOut(BaseModel):
    grid: GridOut
    items: list[ItemOut]
    original_duration_ms: int
    output_duration_ms: int
    removed_ms: int


@router.get("/versions/{version_id}/video/timeline", response_model=TimelineOut)
def get_timeline(
    version_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> TimelineOut:
    """The snap grid and every block, with both lengths the UI must show."""
    settings: Settings = request.app.state.settings
    version = clipping_service.get_version(session, version_id)
    grid = video_service.build_grid(session, version)
    edl = clipping_service.build_edl(session, settings, version)
    keeps = [(keep.orig_start, keep.orig_end) for keep in edl.keeps]
    views = video_service.view_items(session, version.project_id, version, keeps)
    return TimelineOut(
        grid=GridOut(
            boundaries=list(grid.boundaries), targets=list(grid.targets), duration_ms=grid.duration_ms
        ),
        items=[ItemOut(**view.as_dict()) for view in views],  # type: ignore[arg-type]
        original_duration_ms=edl.original_duration_ms,
        output_duration_ms=edl.output_duration_ms,
        removed_ms=edl.removed_ms,
    )


class ItemIn(BaseModel):
    kind: str = "media"
    start_ms: int
    end_ms: int
    fit: str = "blur_fill"
    motion: str = "none"
    z: int = 0
    asset_id: str | None = None
    deck_id: str | None = None
    slide_id: str | None = None
    gallery_json: str = "[]"
    snap: bool = True


class ItemPatchIn(BaseModel):
    kind: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None
    fit: str | None = None
    motion: str | None = None
    z: int | None = None
    asset_id: str | None = None
    deck_id: str | None = None
    slide_id: str | None = None
    gallery_json: str | None = None
    snap: bool = True


def _snapped(
    session: Session, version_id: str, start_ms: int, end_ms: int, *, snap: bool
) -> tuple[int, int]:
    """Snap both edges to the grid when asked.

    The front end also snaps, for immediate feedback; doing it again here is not
    redundant. A client that is one grid revision behind (the user split a sentence
    while a drag was in flight) would otherwise store an edge that never existed as a
    boundary, and the block would look subtly misaligned forever.
    """
    if not snap:
        return start_ms, end_ms
    version = clipping_service.get_version(session, version_id)
    grid = video_service.build_grid(session, version)
    return (
        video_service.snap_edge(start_ms, grid),
        video_service.snap_edge(end_ms, grid),
    )


@router.post("/versions/{version_id}/video/items", response_model=ItemOut, status_code=201)
def create_item(
    version_id: str,
    payload: ItemIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> ItemOut:
    settings: Settings = request.app.state.settings
    version = clipping_service.get_version(session, version_id)
    start, end = _snapped(session, version_id, payload.start_ms, payload.end_ms, snap=payload.snap)
    row = video_service.create_item(
        session,
        version.project_id,
        kind=payload.kind,
        start_ms=start,
        end_ms=end,
        fit=payload.fit,
        motion=payload.motion,
        z=payload.z,
        asset_id=payload.asset_id,
        deck_id=payload.deck_id,
        slide_id=payload.slide_id,
        gallery_json=payload.gallery_json,
    )
    edl = clipping_service.build_edl(session, settings, version)
    keeps = [(keep.orig_start, keep.orig_end) for keep in edl.keeps]
    views = video_service.view_items(session, version.project_id, version, keeps)
    match = next(item for item in views if item.item.id == row.id)
    return ItemOut(**match.as_dict())  # type: ignore[arg-type]


@router.patch("/video/items/{item_id}", response_model=ItemOut)
def patch_item(
    item_id: str,
    payload: ItemPatchIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> ItemOut:
    settings: Settings = request.app.state.settings
    existing = session.get(VideoItem, item_id)
    if existing is None:
        raise AppError("video_item_not_found", "素材块不存在", status=404)

    start = payload.start_ms if payload.start_ms is not None else existing.start_ms
    end = payload.end_ms if payload.end_ms is not None else existing.end_ms
    version = clipping_service.get_active_version(session, existing.project_id)
    start, end = _snapped(session, version.id, start, end, snap=payload.snap)

    row = video_service.update_item(
        session,
        item_id,
        kind=payload.kind,
        start_ms=start,
        end_ms=end,
        fit=payload.fit,
        motion=payload.motion,
        z=payload.z,
        asset_id=payload.asset_id,
        deck_id=payload.deck_id,
        slide_id=payload.slide_id,
        gallery_json=payload.gallery_json,
    )
    edl = clipping_service.build_edl(session, settings, version)
    keeps = [(keep.orig_start, keep.orig_end) for keep in edl.keeps]
    views = video_service.view_items(session, version.project_id, version, keeps)
    match = next(item for item in views if item.item.id == row.id)
    return ItemOut(**match.as_dict())  # type: ignore[arg-type]


@router.delete("/video/items/{item_id}", status_code=204)
def delete_item(item_id: str, session: Session = Depends(_session_dependency)) -> None:
    video_service.delete_item(session, item_id)


# --- preview and export -------------------------------------------------

class SubtitleStyleOut(BaseModel):
    """Everything the 「字幕」 controls can change, round-tripped to the client."""

    font_name: str
    font_size: int
    margin_v: int
    primary_colour: str
    outline: float
    bold: bool


def _style_of(
    font_name: str | None,
    font_size: int | None,
    margin_v: int | None,
    primary_colour: str | None,
    outline: float | None,
    bold: bool | None,
) -> SubtitleStyle:
    """Build a style from the request, defaulting every field it does not set."""
    base = SubtitleStyle()
    return SubtitleStyle(
        font_name=font_name or base.font_name,
        font_size=font_size if font_size is not None else base.font_size,
        margin_v=margin_v if margin_v is not None else base.margin_v,
        primary_colour=primary_colour or base.primary_colour,
        outline=outline if outline is not None else base.outline,
        bold=bold if bold is not None else base.bold,
    )


@router.get("/video/subtitle-style", response_model=SubtitleStyleOut)
def get_subtitle_style() -> SubtitleStyleOut:
    """The default subtitle style, so the panel opens on real values.

    Deliberately **not** version-scoped: the style is a property of the renderer, not
    of a transcript. A version in the path would have been ignored, and a path
    parameter nobody reads is an invitation to pass the wrong one.
    """
    base = SubtitleStyle()
    return SubtitleStyleOut(
        font_name=base.font_name,
        font_size=base.font_size,
        margin_v=base.margin_v,
        primary_colour=base.primary_colour,
        outline=base.outline,
        bold=base.bold,
    )


class PreviewOut(BaseModel):
    url: str
    at_ms: int


@router.get("/versions/{version_id}/video/preview", response_model=PreviewOut)
def preview(
    version_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    at_ms: int = Query(ge=0),
    font_name: str | None = Query(default=None),
    font_size: int | None = Query(default=None, ge=16, le=160),
    margin_v: int | None = Query(default=None, ge=0, le=600),
    primary_colour: str | None = Query(default=None),
) -> PreviewOut:
    """One composited frame at ``at_ms`` (output timeline).

    The style is a query parameter rather than a stored setting because the preview is
    what the user drags *while deciding* the style; saving to 设置 first would make
    every slider move a database write.
    """
    settings: Settings = request.app.state.settings
    version = clipping_service.get_version(session, version_id)
    path = video_render_service.preview_still(
        session,
        settings,
        version.project_id,
        version_id,
        at_ms=at_ms,
        style=_style_of(font_name, font_size, margin_v, primary_colour, None, None),
    )
    return PreviewOut(url=f"/api/video/preview/file/{path.name}", at_ms=at_ms)


@router.get("/video/preview/file/{name}")
def preview_file(
    name: str,
    request: Request,
    project_id: str = Query(),
    session: Session = Depends(_session_dependency),
) -> FileResponse:
    settings: Settings = request.app.state.settings
    directory = video_render_service.output_dir(settings, project_id) / "previews"
    path = directory / Path(name).name
    if not path.is_file():
        raise AppError("preview_missing", "预览图已过期", status=404)
    return FileResponse(path, media_type="image/png")


class VideoExportIn(BaseModel):
    burn_subtitles: bool = True
    font_name: str | None = None
    font_size: int | None = Field(default=None, ge=16, le=160)
    margin_v: int | None = Field(default=None, ge=0, le=600)
    lossless_audio: bool = False


class VideoExportOut(BaseModel):
    id: str
    status: str
    progress: float
    duration_ms: int
    url: str
    error: str | None = None


@router.post("/versions/{version_id}/video/export", response_model=VideoExportOut)
def export_video(
    version_id: str,
    payload: VideoExportIn,
    request: Request,
    session: Session = Depends(_session_dependency),
) -> VideoExportOut:
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    version = clipping_service.get_version(session, version_id)

    options = video_render_service.ExportOptions(
        subtitle_style=_style_of(
            payload.font_name, payload.font_size, payload.margin_v, None, None, None
        )
        if (payload.font_name or payload.font_size or payload.margin_v)
        else None,
        burn_subtitles=payload.burn_subtitles,
        lossless_audio=payload.lossless_audio,
    )
    record = video_render_service.start_export(
        db, settings, version.project_id, version_id, options=options
    )
    return _to_out(record)


@router.get("/video/exports/{render_id}", response_model=VideoExportOut)
def export_status(
    render_id: str, session: Session = Depends(_session_dependency)
) -> VideoExportOut:
    row = session.get(Render, render_id)
    if row is None:
        raise AppError("render_not_found", "导出任务不存在", status=404)
    return _to_out(row)


@router.get("/video/exports/{render_id}/file")
def export_file(
    render_id: str, session: Session = Depends(_session_dependency)
) -> FileResponse:
    row = session.get(Render, render_id)
    if row is None:
        raise AppError("render_not_found", "导出任务不存在", status=404)
    path = Path(row.out_path)
    if not path.is_file():
        raise AppError("export_file_missing", "导出文件已被清理", hint="重新导出即可。", status=404)
    return FileResponse(path, media_type="video/mp4", filename=path.name)


def _to_out(record: Render) -> VideoExportOut:
    done = record.status == "done"
    return VideoExportOut(
        id=record.id,
        status=record.status,
        progress=record.progress,
        duration_ms=record.duration_ms,
        url=f"/api/video/exports/{record.id}/file" if done else "",
        error=record.error,
    )
