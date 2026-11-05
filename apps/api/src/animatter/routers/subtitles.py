"""Subtitle export endpoints.

The important behaviour to preserve: exports are mapped through the project's
TimeMap, so the timestamps match the *rendered* audio rather than the original.
Search exports download a file; preview returns text for the UI to show inline.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from animatter.config import Settings
from animatter.core.edl import TimeMap
from animatter.db import Database
from animatter.errors import AppError
from animatter.services import clipping as clipping_service
from animatter.services import subtitles as subtitle_service

router = APIRouter(prefix="/api", tags=["subtitles"])


def _session_dependency(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.session() as session:
        yield session


class IssueOut(BaseModel):
    sentence_ord: int
    kind: str
    detail: str


class ExportOut(BaseModel):
    id: str
    filename: str
    fmt: str
    url: str
    cue_count: int
    mapped: bool
    issues: list[IssueOut]


def _time_map(session: Session, settings: Settings, version_id: str) -> TimeMap | None:
    """Build the map that puts subtitles on the rendered timeline.

    Returning ``None`` when nothing is deleted keeps the common case exact:
    mapping a timeline onto itself must be a no-op, not a rounding opportunity.
    """
    version = clipping_service.get_version(session, version_id)
    edl = clipping_service.build_edl(session, settings, version)
    if edl.removed_ms == 0:
        return None
    return edl.time_map()


@router.get("/versions/{version_id}/subtitles/preview", response_class=PlainTextResponse)
def preview_subtitles(
    version_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    fmt: str = Query(default="srt"),
    limit: int = Query(default=30, ge=1, le=500),
    mapped: bool = Query(default=True, description="是否映射到成片时间轴"),
) -> PlainTextResponse:
    settings: Settings = request.app.state.settings
    time_map = _time_map(session, settings, version_id) if mapped else None
    text = subtitle_service.preview(session, version_id, fmt=fmt, limit=limit, time_map=time_map)
    return PlainTextResponse(text)


@router.get("/versions/{version_id}/subtitles/issues", response_model=list[IssueOut])
def subtitle_issues(
    version_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    mapped: bool = Query(default=True),
) -> list[IssueOut]:
    """Problems worth fixing before publishing: too fast, too brief, split."""
    settings: Settings = request.app.state.settings
    time_map = _time_map(session, settings, version_id) if mapped else None
    _, issues = subtitle_service.build_cues(session, version_id, time_map=time_map)
    return [
        IssueOut(sentence_ord=issue.sentence_ord, kind=issue.kind, detail=issue.detail) for issue in issues
    ]


@router.post("/versions/{version_id}/subtitles/export", response_model=ExportOut)
def export_subtitles(
    version_id: str,
    request: Request,
    session: Session = Depends(_session_dependency),
    fmt: str = Query(default="srt"),
    mapped: bool = Query(default=True),
    max_chars: int = Query(default=20, ge=8, le=60),
    max_lines: int = Query(default=2, ge=1, le=4),
    max_cps: float = Query(default=9.0, gt=0, le=40),
) -> ExportOut:
    settings: Settings = request.app.state.settings
    time_map = _time_map(session, settings, version_id) if mapped else None
    cues, issues = subtitle_service.build_cues(
        session,
        version_id,
        time_map=time_map,
        max_chars=max_chars,
        max_lines=max_lines,
        max_cps=max_cps,
    )
    row, _ = subtitle_service.export_subtitles(
        session,
        settings,
        version_id,
        fmt=fmt,
        time_map=time_map,
        max_chars=max_chars,
        max_lines=max_lines,
        max_cps=max_cps,
    )
    path = Path(row.path)
    return ExportOut(
        id=row.id,
        filename=path.name,
        fmt=fmt,
        url=f"/api/exports/{row.id}/file",
        cue_count=len(cues),
        mapped=mapped and time_map is not None,
        issues=[IssueOut(sentence_ord=i.sentence_ord, kind=i.kind, detail=i.detail) for i in issues],
    )


@router.get("/exports/{export_id}/file")
def export_file(
    export_id: str,
    session: Session = Depends(_session_dependency),
) -> FileResponse:
    from animatter.models import Export

    row = session.get(Export, export_id)
    if row is None:
        raise AppError("export_not_found", "导出结果不存在", status=404)
    path = Path(row.path)
    if not path.exists():
        raise AppError(
            "export_file_missing",
            "导出文件已被清理",
            hint="重新导出即可。",
            status=404,
        )
    media_type = (
        "application/x-subrip"
        if path.suffix == ".srt"
        else "text/vtt"
        if path.suffix == ".vtt"
        else "text/plain"
    )
    return FileResponse(
        path,
        media_type=f"{media_type}; charset=utf-8",
        filename=path.name,
        content_disposition_type="attachment",
    )
